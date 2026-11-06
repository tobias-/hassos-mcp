import asyncio
import copy
import time

import pytest
from hassio_mcp.changes import Changes
from hassio_mcp.config import Settings

OLD = {"id": "test", "alias": "Before", "triggers": [], "actions": []}
NEW = "id: test\nalias: After\ntriggers: []\nactions: []\n"


class FakeHA:
    def __init__(self):
        self.current = copy.deepcopy(OLD)
        self.writes = 0
        self.reloads = 0
        self.results = ["valid", "valid"]
        self.fail_save = False
        self.deletes = 0

    async def automation(self, automation_id):
        return copy.deepcopy(self.current)

    async def states(self):
        return []

    async def save_automation(self, automation_id, config):
        self.writes += 1
        if self.fail_save:
            raise TimeoutError("simulated ambiguous failure")
        self.current = copy.deepcopy(config)

    async def delete_automation(self, automation_id):
        self.deletes += 1
        if self.fail_save:
            raise TimeoutError("simulated ambiguous deletion")
        self.current = None

    async def check_config(self):
        return {"result": self.results.pop(0)}

    async def reload(self):
        self.reloads += 1


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        yaml_files=("package.yaml",),
        writes_enabled=True,
    )
    settings.config_dir.mkdir()
    ha = FakeHA()
    return Changes(settings, ha), ha, settings


async def test_proposal_does_not_write_and_digest_is_required(fixture):
    changes, ha, _ = fixture
    proposal = await changes.propose("test", NEW)
    assert ha.writes == 0
    assert "-alias: Before" in proposal["diff"]
    with pytest.raises(ValueError, match="digest"):
        await changes.apply(proposal["proposal_id"], "bad")
    assert ha.writes == 0
    await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.current["alias"] == "After"
    assert changes.status(proposal["proposal_id"])["status"] == "applied"
    with pytest.raises(ValueError, match="consumed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.writes == 1


async def test_concurrent_apply_once(fixture):
    changes, ha, _ = fixture
    proposal = await changes.propose("test", NEW)
    results = await asyncio.gather(
        *(changes.apply(proposal["proposal_id"], proposal["digest"]) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert ha.writes == 1


async def test_disabled_expired_and_stale(fixture):
    changes, ha, settings = fixture
    proposal = await changes.propose("test", NEW)
    settings.writes_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    settings.writes_enabled = True
    with changes.db() as db:
        db.execute("UPDATE proposals SET created=?", (time.time() - 1900,))
    with pytest.raises(ValueError, match="expired"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    proposal = await changes.propose("test", NEW)
    ha.current["alias"] = "Someone else's edit"
    with pytest.raises(ValueError, match="changed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.writes == 0


async def test_ambiguous_failure_is_not_retried(fixture):
    changes, ha, _ = fixture
    proposal = await changes.propose("test", NEW)
    ha.fail_save = True
    with pytest.raises(TimeoutError):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    with pytest.raises(ValueError, match="consumed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert changes.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"
    assert ha.writes == 1


async def test_create_never_overwrites(fixture):
    changes, ha, _ = fixture
    with pytest.raises(ValueError, match="Create/edit"):
        await changes.propose("test", NEW, create=True)
    ha.current = None
    proposal = await changes.propose("test", NEW, create=True)
    await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.writes == 1


async def test_yaml_preserves_other_sections_tags_and_rolls_back(fixture):
    changes, ha, settings = fixture
    path = settings.config_dir / "package.yaml"
    original = "# Package comment\nsensor: !include sensors.yaml\nautomation:\n  - id: test\n    alias: Before\n    triggers: []\n    actions: []\n"
    path.write_text(original)
    proposal = await changes.propose("test", NEW, "package.yaml")
    assert path.read_text() == original
    ha.results = ["valid", "invalid"]
    with pytest.raises(ValueError, match="rejected"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert path.read_text() == original
    assert ha.reloads == 0
    ha.results = ["valid", "valid"]
    proposal = await changes.propose("test", NEW, "package.yaml")
    await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert "!include sensors.yaml" in path.read_text()
    assert "# Package comment" in path.read_text()
    assert "alias: After" in path.read_text()
    assert ha.reloads == 1


def test_yaml_path_boundaries(fixture, tmp_path):
    changes, _, settings = fixture
    for name in (
        "../secrets.yaml",
        "/etc/passwd",
        "secrets.yaml",
        "configuration.yaml",
    ):
        with pytest.raises(ValueError):
            changes.files.path(name)
    outside = tmp_path / "outside.yaml"
    outside.write_text("[]")
    (settings.config_dir / "package.yaml").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        changes.files.path("package.yaml")


async def test_yaml_external_edit_prevents_write(fixture):
    changes, ha, settings = fixture
    path = settings.config_dir / "package.yaml"
    path.write_text("- id: test\n  triggers: []\n  actions: []\n")
    proposal = await changes.propose("test", NEW, "package.yaml")
    path.write_text("# external change\n" + path.read_text())
    with pytest.raises(ValueError, match="changed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert path.read_text().startswith("# external change")
    assert ha.reloads == 0


async def test_api_deletion_requires_accepted_digest_and_is_single_use(fixture):
    changes, ha, settings = fixture
    proposal = await changes.propose("test", None, delete=True)
    assert proposal["operation"] == "delete"
    assert proposal["approval_required"] is True
    assert "-alias: Before" in proposal["diff"]
    assert ha.current == OLD and ha.deletes == 0
    settings.writes_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    settings.writes_enabled = True
    with pytest.raises(ValueError, match="digest"):
        await changes.apply(proposal["proposal_id"], "unaccepted")
    assert ha.deletes == 0
    await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.current is None
    assert changes.status(proposal["proposal_id"])["operation"] == "delete"
    with pytest.raises(ValueError, match="consumed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.deletes == 1


async def test_delete_rejects_stale_and_ambiguous_failure_is_not_retried(fixture):
    changes, ha, _ = fixture
    proposal = await changes.propose("test", None, delete=True)
    ha.current["alias"] = "Edited elsewhere"
    with pytest.raises(ValueError, match="changed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.deletes == 0
    proposal = await changes.propose("test", None, delete=True)
    ha.fail_save = True
    with pytest.raises(TimeoutError):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert changes.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"
    with pytest.raises(ValueError, match="consumed"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.deletes == 1


async def test_yaml_create_delete_preserves_package_and_rolls_back_on_rejection(
    fixture,
):
    changes, ha, settings = fixture
    ha.current = None
    path = settings.config_dir / "package.yaml"
    original = "# Keep package\nsensor: !include sensors.yaml\nautomation: []\n"
    path.write_text(original)
    proposal = await changes.propose("test", NEW, "package.yaml", create=True)
    assert path.read_text() == original
    await changes.apply(proposal["proposal_id"], proposal["digest"])
    created = path.read_text()
    assert "id: test" in created
    assert "sensor: !include sensors.yaml" in created
    assert ha.reloads == 1
    delete = await changes.propose("test", None, "package.yaml", delete=True)
    assert path.read_text() == created
    ha.results = ["valid", "invalid"]
    with pytest.raises(ValueError, match="rejected"):
        await changes.apply(delete["proposal_id"], delete["digest"])
    assert path.read_text() == created
    assert ha.reloads == 1
    delete = await changes.propose("test", None, "package.yaml", delete=True)
    ha.results = ["valid", "valid"]
    await changes.apply(delete["proposal_id"], delete["digest"])
    assert "id: test" not in path.read_text()
    assert "sensor: !include sensors.yaml" in path.read_text()
    assert "# Keep package" in path.read_text()
    assert ha.reloads == 2


async def test_yaml_create_rejects_id_collisions_before_proposal_and_apply(fixture):
    changes, ha, settings = fixture
    path = settings.config_dir / "package.yaml"
    path.write_text("[]\n")
    with pytest.raises(ValueError, match="already exists"):
        await changes.propose("test", NEW, "package.yaml", create=True)
    ha.current = None
    proposal = await changes.propose("test", NEW, "package.yaml", create=True)
    ha.current = OLD
    with pytest.raises(ValueError, match="already exists"):
        await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert path.read_text() == "[]\n"
    assert ha.reloads == 0
