import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect

from .config import Settings, validate_url

MAX_RESPONSE = 8 * 1024 * 1024


def entity(value: str, domain: str | None = None):
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value):
        raise ValueError("Invalid entity ID")
    if domain and not value.startswith(domain + "."):
        raise ValueError(f"Expected a {domain} entity")
    return value


def identifier(value: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid automation ID")
    return value


def time_range(start: str, end: str, max_days: int = 31):
    dates = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end)]
    if any(d.tzinfo is None for d in dates):
        raise ValueError("Use ISO timestamps with an explicit timezone")
    if not 0 < (dates[1] - dates[0]).total_seconds() <= max_days * 86400:
        raise ValueError(f"Request a positive interval no longer than {max_days} days")
    return tuple(d.astimezone(UTC).isoformat() for d in dates)


class HAError(RuntimeError):
    def __init__(self, status: int | str):
        self.status = status
        super().__init__(
            f"Home Assistant request failed ({status}); check connectivity, token, and permissions"
        )


class HomeAssistant:
    def __init__(self, settings: Settings):
        self.settings = settings
        validate_url(settings.ha_url)

    async def request(
        self, method: str, path: str, *, params=None, body=None, text=False
    ):
        if not self.settings.ha_token:
            raise ValueError("HA_TOKEN is not configured")
        async with httpx.AsyncClient(
            verify=self.settings.tls(),
            timeout=30,
            trust_env=False,
            headers={"Authorization": f"Bearer {self.settings.ha_token}"},
        ) as client:
            try:
                async with client.stream(
                    method, self.settings.ha_url + path, params=params, json=body
                ) as response:
                    if response.status_code >= 300:
                        raise HAError(response.status_code)
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_RESPONSE:
                            raise ValueError(
                                "Response too large; narrow the time range or entity selection"
                            )
                    return chunks.decode() if text else json.loads(chunks)
            except httpx.HTTPError:
                raise HAError("transport") from None

    async def ws(self, command: str, **params):
        if not self.settings.ha_token:
            raise ValueError("HA_TOKEN is not configured")
        url = (
            self.settings.ha_url.replace("https://", "wss://", 1).replace(
                "http://", "ws://", 1
            )
            + "/api/websocket"
        )
        kwargs = {"ssl": self.settings.tls()} if url.startswith("wss:") else {}
        async with asyncio.timeout(30):
            async with connect(url, max_size=MAX_RESPONSE, proxy=None, **kwargs) as ws:
                if json.loads(await ws.recv()).get("type") != "auth_required":
                    raise HAError("unexpected authentication handshake")
                await ws.send(
                    json.dumps({"type": "auth", "access_token": self.settings.ha_token})
                )
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    raise HAError("authentication")
                await ws.send(json.dumps({"id": 1, "type": command, **params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == 1:
                        if not message.get("success"):
                            raise HAError(
                                message.get("error", {}).get("code", "websocket")
                            )
                        return message.get("result")

    async def states(self):
        return await self.request("GET", "/api/states")

    async def automation(self, automation_id):
        try:
            return await self.request(
                "GET",
                "/api/config/automation/config/"
                + quote(identifier(automation_id), safe=""),
            )
        except HAError as exc:
            if exc.status == 404:
                return None
            raise

    async def save_automation(self, automation_id, config):
        return await self.request(
            "POST",
            "/api/config/automation/config/" + identifier(automation_id),
            body=config,
        )

    async def delete_automation(self, automation_id):
        return await self.request(
            "DELETE", "/api/config/automation/config/" + identifier(automation_id)
        )

    async def check_config(self):
        return await self.request("POST", "/api/config/core/check_config", body={})

    async def reload(self):
        return await self.request("POST", "/api/services/automation/reload", body={})
