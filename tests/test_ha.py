import json

import httpx
import pytest
from websockets.asyncio.server import serve

from hassio_mcp.config import Settings
from hassio_mcp.ha import HAError, HomeAssistant


async def test_rest_auth_no_redirects_and_private_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/api/states":
            return httpx.Response(
                200, json=[{"entity_id": "sensor.test", "state": "42"}]
            )
        return httpx.Response(
            302,
            headers={"location": "https://untrusted.invalid/"},
            text="private server error",
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    ha = HomeAssistant(Settings(ha_token="test-secret"))
    assert (await ha.states())[0]["state"] == "42"
    with pytest.raises(HAError) as exc:
        await ha.request("GET", "/redirect")
    assert "private server error" not in str(exc.value)
    assert len(calls) == 2


async def test_websocket_auth_and_command_dispatch():
    observed = []

    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        observed.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "auth_ok"}))
        command = json.loads(await ws.recv())
        observed.append(command)
        await ws.send(
            json.dumps(
                {
                    "id": command["id"],
                    "success": True,
                    "result": {"config": {"alias": "Test"}},
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ha = HomeAssistant(
            Settings(ha_url=f"http://127.0.0.1:{port}", ha_token="test-secret")
        )
        result = await ha.ws("automation/config", entity_id="automation.test")
    assert result["config"]["alias"] == "Test"
    assert observed[0] == {"type": "auth", "access_token": "test-secret"}
    assert observed[1] == {
        "id": 1,
        "type": "automation/config",
        "entity_id": "automation.test",
    }
