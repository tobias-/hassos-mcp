import copy

import httpx
import pytest
from hassio_mcp.actions import Actions
from hassio_mcp.config import Settings
from hassio_mcp.diagnostics import Diagnostics, levels, scrub_lines
from hassio_mcp.ha import HAError, HomeAssistant
from hassio_mcp.review import ReviewStore


class HA:
    def __init__(self):
        self.writes = []
        self.source = "core"
        self.fail = False
        self.raw = "INFO [zigpy] 00:11:22:33:44:55:66:77 received\nINFO [other] unrelated\nDEBUG [zigpy] network_key=private\n"

    async def core_logs(self, lines):
        if self.source != "core":
            raise HAError(404)
        return self.raw, False

    async def request(self, method, path, **kwargs):
        if path == "/api/services":
            return [{"domain": "logger", "services": {"set_level": {"fields": {}}}}]
        if method == "POST":
            self.writes.append(copy.deepcopy(kwargs["body"]))
            if self.fail:
                raise HAError("transport")
            return []
        if self.source != "legacy":
            raise HAError(404)
        return self.raw

    async def ws(self, command):
        assert command == "system_log/list"
        return [{"name": "zigpy", "message": "warning"}]


@pytest.fixture
def manager(tmp_path):
    settings = Settings(
        data_dir=tmp_path, zha_debug_enabled=True, management_writes_enabled=True
    )
    return Diagnostics(settings, HA(), ReviewStore(settings))


async def accepted(manager):
    p = await manager.propose({"zigpy": "info"}, 30)
    return p, await manager.apply(p["proposal_id"], p["digest"])


async def test_approval_digest_switch_expiry_replay_and_timer(manager):
    p = await manager.propose({"zigpy": "info"}, 30)
    assert not manager.ha.writes and p["approval_required"]
    with pytest.raises(ValueError, match="digest"):
        await manager.apply(p["proposal_id"], "wrong")
    manager.settings.zha_debug_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await manager.apply(p["proposal_id"], p["digest"])
    manager.settings.zha_debug_enabled = True
    await manager.apply(p["proposal_id"], p["digest"])
    assert manager.ha.writes == [{"zigpy": "debug"}]
    with pytest.raises(ValueError, match="consumed"):
        await manager.apply(p["proposal_id"], p["digest"])
    await manager.tick()
    assert manager.status(p["proposal_id"])["lines"]
    with manager.reviews.db() as db:
        db.execute("UPDATE debug_captures SET deadline=0")
    manager.settings.zha_debug_enabled = False
    await manager.tick()
    assert manager.ha.writes == [{"zigpy": "debug"}, {"zigpy": "info"}]
    assert manager.status(p["proposal_id"])["status"] == "restored"
    await manager.tick()
    assert len(manager.ha.writes) == 2
    p = await manager.propose({"zigpy": "info"})
    with manager.reviews.db() as db:
        db.execute("UPDATE proposals SET created=0 WHERE id=?", (p["proposal_id"],))
    manager.settings.zha_debug_enabled = True
    with pytest.raises(ValueError, match="expired"):
        await manager.apply(p["proposal_id"], p["digest"])


async def test_persisted_deadline_and_unknown_restore_never_retried(manager):
    p, _ = await accepted(manager)
    other = await manager.propose({"zha": "info"})
    with pytest.raises(ValueError, match="active/uncertain"):
        await manager.apply(other["proposal_id"], other["digest"])
    with manager.reviews.db() as db:
        db.execute("UPDATE debug_captures SET deadline=0")
    restarted = Diagnostics(manager.settings, manager.ha, manager.reviews)
    manager.ha.fail = True
    await restarted.tick()
    assert restarted.status(p["proposal_id"])["status"] == "restore_uncertain"
    await restarted.tick()
    assert len(manager.ha.writes) == 2
    proposal = await restarted.propose_restore(p["proposal_id"])
    assert len(manager.ha.writes) == 2
    manager.ha.fail = False
    await restarted.apply_restore(proposal["proposal_id"], proposal["digest"])
    assert manager.ha.writes[-1] == {"zigpy": "info"}
    assert restarted.status(p["proposal_id"])["status"] == "restored"


async def test_unknown_start_still_restores_and_shutdown_cleans_up(manager):
    p = await manager.propose({"zigpy": "warning"})
    manager.ha.fail = True
    with pytest.raises(HAError):
        await manager.apply(p["proposal_id"], p["digest"])
    assert manager.status(p["proposal_id"])["status"] == "starting"
    manager.ha.fail = False
    async with manager.lifespan(None):
        pass
    assert manager.status(p["proposal_id"])["status"] == "restored"
    assert manager.ha.writes[-1] == {"zigpy": "warning"}


async def test_logs_fallback_filter_bounds_and_redaction(manager):
    result = await manager.logs(10, "00:11:22:33:44:55:66:77")
    assert len(result["lines"]) == 1 and result["source"] == "supervisor_core"
    assert "private" not in str(await manager.logs(100, zigbee_only=False))
    manager.ha.raw = ("[zigpy] " + "a" * 4000 + "\n") * 1000
    assert len(str((await manager.logs(500))["lines"]).encode()) < 66000
    assert (await manager.logs(500))["truncated"]
    manager.ha.source = "legacy"
    assert (await manager.logs())["source"] == "legacy_error_log"
    manager.ha.source = "warning"
    assert (await manager.logs())["source"] == "system_log_warning_error_only"
    with pytest.raises(ValueError, match="Full Core logs"):
        await manager.propose({"zigpy": "info"})
    assert not manager.ha.writes


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"root": "info"},
        {"zigpy_evil": "info"},
        {"zigpy": "no"},
        {"homeassistant": "info"},
        {"zigpy.foo;reset": "info"},
    ],
)
def test_logger_scope(value):
    with pytest.raises(ValueError):
        levels(value)


def test_pem_and_known_credentials_never_returned(manager):
    manager.settings.ha_token = "unique-runtime-token"
    result = scrub_lines(
        "BEGIN\n-----BEGIN PRIVATE KEY-----\nkeymaterial\n-----END PRIVATE KEY-----\n[zigpy] unique-runtime-token\n[zigpy] safe",
        manager.settings,
    )
    assert "keymaterial" not in str(result) and "unique-runtime-token" not in str(
        result
    )
    assert result[-1] == "[zigpy] safe"


async def test_dynamic_logger_actions_are_scoped_reviewed_and_debug_is_timed(manager):
    actions = Actions(manager.ha, manager.reviews)
    p = await actions.propose("logger", "set_level", data={"zigpy": "warning"})
    assert not manager.ha.writes
    await actions.apply(p["proposal_id"], p["digest"])
    assert manager.ha.writes == [{"zigpy": "warning"}]
    for data in (
        {"zigpy": "debug"},
        {"root": "info"},
        {"homeassistant.components.zha": "INVALID"},
    ):
        with pytest.raises(ValueError):
            await actions.propose("logger", "set_level", data=data)


async def test_core_log_endpoint_auth_and_bounds(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, content=b"[zigpy] safe\n" + b"x" * (1024 * 1024 + 100)
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "hassio_mcp.ha.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(respond), **kwargs),
    )
    raw, truncated = await HomeAssistant(Settings()).core_logs()
    assert truncated and len(raw.encode()) <= 1024 * 1024
    assert str(seen[0].url).startswith("http://supervisor/core/logs?")
    assert seen[0].headers["Authorization"] == "Bearer test-supervisor-token"
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    await HomeAssistant(
        Settings(ha_url="http://ha:8123", ha_token="manual-token")
    ).core_logs()
    assert str(seen[-1].url).startswith("http://ha:8123/api/hassio/core/logs?")
    assert seen[-1].headers["Authorization"] == "Bearer manual-token"
