import asyncio
import os
import time

import pytest
from hassio_mcp.blueprints import Blueprints, parse_blueprint
from hassio_mcp.config import Settings
from hassio_mcp.ha import HAError
from ruamel.yaml.error import YAMLError

OLD = """# Existing blueprint comment
blueprint:
  name: Before
  domain: automation
  input:
    target:
      name: Target
triggers: []
actions:
  - action: light.turn_on
    target:
      entity_id: !input target
"""
NEW = OLD.replace("Before", "After")
PATH = "local/test.yaml"


class FakeHA:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.failure = None
        self.alter_saved = False

    async def ws(self, command, **params):
        self.calls.append((command, params))
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        if command == "blueprint/list":
            return {PATH: {"metadata": {"name": "Before", "domain": params["domain"]}}}
        target = self.root / "blueprints" / params["domain"] / params["path"]
        if command == "blueprint/save":
            if target.exists() and not params["allow_override"]:
                raise HAError("already_exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = params["yaml"]
            if self.alter_saved:
                source = source.replace("After", "Unexpected")
            target.write_text(source)
            return {"overrides_existing": params["allow_override"]}
        if command == "blueprint/delete":
            target.unlink()
            return None
        raise AssertionError(command)


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        blueprint_writes_enabled=True,
    )
    file = settings.config_dir / "blueprints/automation" / PATH
    file.parent.mkdir(parents=True)
    file.write_text(OLD)
    ha = FakeHA(settings.config_dir)
    return Blueprints(settings, ha), ha, settings, file


async def test_list_read_proposal_acceptance_and_persisted_backup(fixture):
    box, ha, settings, file = fixture
    assert (await box.list("automation"))[0]["path"] == PATH
    assert box.read("automation", PATH) == OLD
    ha.calls.clear()
    proposed = await box.propose("update", "automation", PATH, NEW)
    assert not ha.calls
    assert file.read_text() == OLD
    assert "-  name: Before" in proposed["diff"]
    assert proposed["approval_required"] is True
    with pytest.raises(ValueError, match="digest"):
        await box.apply(proposed["proposal_id"], "wrong")
    assert not ha.calls
    # Proposals and backups survive a new server instance.
    reopened = Blueprints(settings, ha)
    await reopened.apply(proposed["proposal_id"], proposed["digest"])
    assert file.read_text() == NEW
    assert ha.calls == [
        (
            "blueprint/save",
            {"domain": "automation", "path": PATH, "yaml": NEW, "allow_override": True},
        )
    ]
    status = reopened.status(proposed["proposal_id"])
    assert status["status"] == "applied"
    assert status["before"] == OLD
    assert status["after"] == NEW
    with pytest.raises(ValueError, match="consumed"):
        await reopened.apply(proposed["proposal_id"], proposed["digest"])


@pytest.mark.parametrize(
    "domain,body",
    [
        ("automation", "triggers: []\nactions: []\n"),
        ("script", "sequence: []\n"),
        ("template", "sensor: []\n"),
    ],
)
async def test_create_delete_each_need_separate_proposal(fixture, domain, body):
    box, ha, _, _ = fixture
    path = "local/new.yaml"
    yaml = f"blueprint:\n  name: Test\n  domain: {domain}\n" + body
    create = await box.propose("create", domain, path, yaml)
    assert box.read(domain, path) is None
    await box.apply(create["proposal_id"], create["digest"])
    assert ha.calls[-1][1]["allow_override"] is False
    assert box.read(domain, path) == yaml
    delete = await box.propose("delete", domain, path)
    assert box.read(domain, path) == yaml
    with pytest.raises(ValueError, match="digest"):
        await box.apply(delete["proposal_id"], create["digest"])
    await box.apply(delete["proposal_id"], delete["digest"])
    assert box.read(domain, path) is None
    assert box.status(delete["proposal_id"])["before"] == yaml


async def test_disabled_expired_stale_and_concurrent_replay(fixture):
    box, ha, settings, file = fixture
    proposal = await box.propose("update", "automation", PATH, NEW)
    settings.blueprint_writes_enabled = False
    settings.writes_enabled = (
        True  # Automation permission must not grant blueprint access.
    )
    with pytest.raises(ValueError, match="disabled"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    settings.blueprint_writes_enabled = True
    with box.db() as db:
        db.execute("UPDATE proposals SET created=?", (time.time() - 1900,))
    with pytest.raises(ValueError, match="expired"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    proposal = await box.propose("update", "automation", PATH, NEW)
    file.write_text(OLD + "# External change\n")
    with pytest.raises(ValueError, match="changed"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    assert not ha.calls
    proposal = await box.propose("update", "automation", PATH, NEW)
    second_instance = Blueprints(settings, ha)
    results = await asyncio.gather(
        box.apply(proposal["proposal_id"], proposal["digest"]),
        second_instance.apply(proposal["proposal_id"], proposal["digest"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(ha.calls) == 1


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_api_failure_is_uncertain_and_never_falls_back(fixture, operation):
    box, ha, _, file = fixture
    proposal = await box.propose(
        operation, "automation", PATH, NEW if operation == "update" else None
    )
    ha.failure = HAError(
        "blueprint_in_use" if operation == "delete" else "invalid_format"
    )
    with pytest.raises(HAError):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    assert file.read_text() == OLD
    assert box.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"
    with pytest.raises(ValueError, match="consumed"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    assert len(ha.calls) == 1


async def test_verification_failure_preserves_backup_without_rollback(fixture):
    box, ha, _, file = fixture
    proposal = await box.propose("update", "automation", PATH, NEW)
    ha.alter_saved = True
    with pytest.raises(RuntimeError, match="differs"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    assert "Unexpected" in file.read_text()
    assert box.status(proposal["proposal_id"])["before"] == OLD
    assert box.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"
    assert len(ha.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.yaml",
        "/etc/test.yaml",
        "a/../../test.yaml",
        "a//test.yaml",
        "a/./test.yaml",
        "secrets.yaml",
        "test.yml",
        "a\\test.yaml",
        "a/test.yaml\x00",
    ],
)
def test_path_boundaries(fixture, path):
    with pytest.raises(ValueError):
        fixture[0].read("automation", path)


async def test_missing_mount_symlinks_and_create_collision(fixture, tmp_path):
    box, ha, settings, file = fixture
    with pytest.raises(ValueError, match="absent"):
        await box.propose("create", "automation", PATH, NEW)
    with pytest.raises(ValueError, match="existing"):
        await box.propose("delete", "automation", "missing.yaml")
    proposal = await box.propose("delete", "automation", PATH)
    file.unlink()
    outside = tmp_path / "outside.yaml"
    outside.write_text(OLD)
    file.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        await box.apply(proposal["proposal_id"], proposal["digest"])
    assert outside.read_text() == OLD
    missing = Blueprints(
        Settings(data_dir=settings.data_dir, config_dir=tmp_path / "missing"), ha
    )
    with pytest.raises(ValueError, match="Mount"):
        missing.read("automation", PATH)
    # Remote listing still works without a mount.
    assert (await missing.list("automation"))[0]["path"] == PATH


@pytest.mark.parametrize(
    "source",
    [
        "[]",
        "blueprint: {}",
        OLD.replace("automation", "script"),
        OLD + "extra: !include /etc/passwd\n",
        OLD + "extra: !secret password\n",
        OLD + "extra: !unknown thing\n",
        OLD + "actions: []\n",
        "x" * (128 * 1024 + 1),
    ],
)
def test_invalid_yaml(fixture, source):
    with pytest.raises((ValueError, YAMLError)):
        parse_blueprint(source, "automation")


def test_non_regular_files_rejected_without_blocking(fixture):
    box, _, _, file = fixture
    file.unlink()
    os.mkfifo(file)
    with pytest.raises(ValueError, match="regular"):
        box.read("automation", PATH)


async def test_diff_marks_missing_final_newline(fixture):
    box, _, _, file = fixture
    file.write_text(OLD.rstrip("\n"))
    proposal = await box.propose("update", "automation", PATH, NEW)
    assert "\\ No newline at end of file\n" in proposal["diff"]


def test_yaml_verification_handles_input_tags_and_defaults():
    assert parse_blueprint(OLD, "automation") == parse_blueprint(
        OLD.replace("# Existing blueprint comment\n", ""), "automation"
    )
    assert parse_blueprint(OLD, "automation") != parse_blueprint(
        OLD.replace("!input target", "['!input', 'target']"), "automation"
    )
    no_inputs = "blueprint:\n  name: Test\n  domain: script\nsequence: []\n"
    assert parse_blueprint(no_inputs, "script") == parse_blueprint(
        no_inputs.replace("  domain: script", "  domain: script\n  input: {}"), "script"
    )


def test_configuration_toggle_is_independent(monkeypatch):
    monkeypatch.setenv("ALLOW_AUTOMATION_WRITES", "true")
    monkeypatch.delenv("ALLOW_BLUEPRINT_WRITES", raising=False)
    assert not Settings.from_env().blueprint_writes_enabled
    monkeypatch.setenv("ALLOW_BLUEPRINT_WRITES", "true")
    assert Settings.from_env().blueprint_writes_enabled
