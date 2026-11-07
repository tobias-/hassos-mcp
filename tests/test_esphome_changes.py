import os
import time

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.esphome_changes import ESPHomeChanges
from hassio_mcp.esphome_runtime import ESPHomeRuntime
from hassio_mcp.review import ReviewStore

SOURCE = "esphome:\n  name: controller\nesp8266:\n  board: esp01_1m\nwifi:\n  password: !secret wifi_password\n# original wiring\n"
AFTER = SOURCE + "switch:\n  - platform: gpio\n    pin: GPIO5\n"


@pytest.fixture
def changes(tmp_path):
    root = tmp_path / "esphome"
    root.mkdir()
    (root / "controller.yaml").write_text(SOURCE)
    (root / "controller.yaml").chmod(0o640)
    (root / "secrets.yaml").write_text("wifi_password: a-private-password\n")
    settings = Settings(
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        esphome_config_writes_enabled=True,
    )
    runtime = ESPHomeRuntime(settings)

    async def execute(*args, **kwargs):
        assert args[1] == "validate"  # Never implicitly compile, install or restart.
        return {"success": True, "status": "succeeded", "version": "2026.10.2"}

    runtime.execute = execute
    return ESPHomeChanges(runtime, ReviewStore(settings))


async def propose(changes):
    current = changes.runtime.files.get_config("controller")
    result = await changes.propose("controller", AFTER, current["sha256"])
    assert result["success"], result
    return result


async def test_exact_review_backup_atomic_write_permissions_and_rollback(changes):
    path = changes.runtime.files.root / "controller.yaml"
    proposal = await propose(changes)
    assert path.read_text() == SOURCE
    assert "+    pin: GPIO5" in proposal["diff"]
    assert "a-private-password" not in str(proposal)
    assert proposal["content_sha256"] != proposal["expected_sha256"]
    result = await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert result["success"] and path.read_text() == AFTER
    assert path.stat().st_mode & 0o777 == 0o640
    assert result["backup_reference"] == proposal["proposal_id"]
    status = await changes.status(proposal["proposal_id"])
    assert status["before"] == SOURCE and status["status"] == "applied"
    rollback = await changes.rollback(proposal["proposal_id"])
    assert rollback["success"] and rollback["after"] == SOURCE
    assert path.read_text() == AFTER
    assert (await changes.apply(rollback["proposal_id"], rollback["digest"]))["success"]
    assert path.read_text() == SOURCE


@pytest.mark.parametrize(
    "change", ["hash", "secret", "expired", "digest", "permission", "replay"]
)
async def test_rejects_stale_unapproved_expired_and_replayed_changes(changes, change):
    proposal = await propose(changes)
    path = changes.runtime.files.root / "controller.yaml"
    digest = proposal["digest"]
    if change == "hash":
        path.write_text(SOURCE + "# external edit\n")
    elif change == "secret":
        (changes.runtime.files.root / "secrets.yaml").write_text(
            "wifi_password: new-private-password\n"
        )
    elif change == "expired":
        with changes.reviews.db() as db:
            db.execute("UPDATE proposals SET created=?", (time.time() - 2000,))
    elif change == "digest":
        digest = "not-approved"
    elif change == "permission":
        changes.settings.esphome_config_writes_enabled = False
    elif change == "replay":
        assert (await changes.apply(proposal["proposal_id"], digest))["success"]
    before = path.read_bytes()
    result = await changes.apply(proposal["proposal_id"], digest)
    assert result["success"] is False
    assert path.read_bytes() == before


async def test_failed_validation_never_creates_proposal(changes):
    async def fail(*args, **kwargs):
        return {
            "success": False,
            "error": {
                "code": "CONFIG_INVALID",
                "message": "Invalid pin",
                "retryable": False,
            },
        }

    changes.runtime.execute = fail
    current = changes.runtime.files.get_config("controller")
    result = await changes.propose("controller", AFTER, current["sha256"])
    assert result["error"]["code"] == "CONFIG_INVALID"
    with changes.reviews.db() as db:
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


async def test_uncertain_replace_is_consumed_and_backup_remains(changes, monkeypatch):
    proposal = await propose(changes)
    original = os.fsync

    def fail_directory(fd):
        import stat

        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated post-replacement durability error")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    result = await changes.apply(proposal["proposal_id"], proposal["digest"])
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    status = await changes.status(proposal["proposal_id"])
    assert status["status"] == "failed_or_uncertain" and status["before"] == SOURCE
    assert not (await changes.apply(proposal["proposal_id"], proposal["digest"]))[
        "success"
    ]
