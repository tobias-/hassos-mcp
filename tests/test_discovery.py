import pytest
from hassio_mcp.discovery import discover


class HA:
    async def ws(self, command):
        return [
            {
                "entry_id": "test",
                "entity_id": "light.test",
                "domain": "light",
                "title": "Lights",
                "state": "loaded",
                "data": {"password": "never-return"},
                "options": {"token": "never-return"},
                "credentials": "never-return",
            }
        ]


@pytest.mark.parametrize("kind", ["entities", "devices", "areas", "integrations"])
async def test_registry_only_returns_public_metadata(kind):
    rows = await discover(HA(), kind)
    assert "never-return" not in str(rows)
    assert "data" not in rows[0]
