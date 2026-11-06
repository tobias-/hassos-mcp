import os

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.config_files import ConfigFiles
from hassio_mcp.review import ReviewStore


class HA:
    def __init__(self):
        self.valid = True
        self.reloads = []
        self.on_check = None

    async def states(self):
        return []

    async def check_config(self):
        if self.on_check:
            self.on_check()
        return {"result": "valid" if self.valid else "invalid"}

    async def request(self, method, path, **kwargs):
        self.reloads.append((method, path))
        return []


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        config_files=("test.yaml", "new.yaml", "secrets.yaml", "../outside.yaml"),
        management_writes_enabled=True,
    )
    settings.config_dir.mkdir()
    path = settings.config_dir / "test.yaml"
    path.write_text("script: {}\n")
    ha = HA()
    reviews = ReviewStore(settings)
    return ConfigFiles(settings, ha, reviews), ha, path, reviews


async def test_config_change_validate_reload_and_reviewed_rollback(fixture):
    files, ha, path, reviews = fixture
    old = path.read_text()
    proposal = await files.propose(
        "test.yaml", "script:\n  test:\n    sequence: []\n", "script"
    )
    assert path.read_text() == old and not ha.reloads
    with pytest.raises(ValueError, match="digest"):
        await files.apply(proposal["proposal_id"], "wrong")
    await files.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.reloads == [("POST", "/api/services/script/reload")]
    rollback = await files.rollback(proposal["proposal_id"])
    assert path.read_text() != old
    await files.apply(rollback["proposal_id"], rollback["digest"])
    assert path.read_text() == old
    assert reviews.status(rollback["proposal_id"])["status"] == "applied"


async def test_rejected_config_restored_before_reload(fixture):
    files, ha, path, reviews = fixture
    old = path.read_text()
    proposal = await files.propose(
        "test.yaml", "script: {test: {sequence: []}}", "script"
    )
    ha.valid = False
    with pytest.raises(ValueError, match="rejected"):
        await files.apply(proposal["proposal_id"], proposal["digest"])
    assert path.read_text() == old and not ha.reloads
    assert reviews.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"


async def test_external_edit_never_overwritten_by_validation_rollback(fixture):
    files, ha, path, _ = fixture
    proposal = await files.propose("test.yaml", "script: {test: {sequence: []}}")
    ha.valid = False
    ha.on_check = lambda: path.write_text("# Changed externally\nscript: {}")
    with pytest.raises(RuntimeError, match="changed externally"):
        await files.apply(proposal["proposal_id"], proposal["digest"])
    assert "Changed externally" in path.read_text()


async def test_create_delete_and_stale_file(fixture):
    files, _, path, _ = fixture
    create = await files.propose("new.yaml", "[]\n")
    await files.apply(create["proposal_id"], create["digest"])
    delete = await files.propose("new.yaml", None)
    await files.apply(delete["proposal_id"], delete["digest"])
    assert files.read("new.yaml") is None
    proposal = await files.propose("test.yaml", "script: {test: {sequence: []}}")
    path.write_text("script: {} # changed")
    with pytest.raises(ValueError, match="changed"):
        await files.apply(proposal["proposal_id"], proposal["digest"])


def test_secret_paths_inline_credentials_symlinks_and_fifos_excluded(fixture, tmp_path):
    files, _, path, _ = fixture
    for name in (
        "secrets.yaml",
        "../outside.yaml",
        ".storage/auth",
        "/etc/passwd",
        "blueprints/automation/test.yaml",
    ):
        with pytest.raises(ValueError):
            files.read(name)
    path.write_text("integration:\n  password: do-not-return\n")
    with pytest.raises(ValueError, match="Inline credential"):
        files.read("test.yaml")
    path.write_text("integration:\n  password: !secret integration_password\n")
    assert "!secret" in files.read("test.yaml")
    path.unlink()
    outside = tmp_path / "outside.yaml"
    outside.write_text("script: {}")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        files.read("test.yaml")
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        files.read("test.yaml")
