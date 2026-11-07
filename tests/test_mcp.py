import hashlib
import json
import os
import sys
from importlib.metadata import version

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_discovery_and_missing_credentials(tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in ("HA_TOKEN", "SUPERVISOR_TOKEN")
    }
    env["DATA_DIR"] = str(tmp_path)
    config_dir = tmp_path / "config"
    blueprint = config_dir / "blueprints/automation/local/test.yaml"
    blueprint.parent.mkdir(parents=True)
    blueprint.write_text(
        "blueprint:\n  name: Test\n  domain: automation\ntriggers: []\nactions: []\n"
    )
    env["HA_CONFIG_DIR"] = str(config_dir)
    esphome_dir = config_dir / "esphome"
    esphome_dir.mkdir()
    (esphome_dir / "solder-table.yaml").write_text(
        "esphome:\n  name: solder-table\nesp8266:\n  board: nodemcuv2\nswitch:\n  - platform: gpio\n    pin: GPIO5 # relay\n"
    )
    env["ESPHOME_CONFIG_DIR"] = str(esphome_dir)
    env["ALLOW_BLUEPRINT_WRITES"] = "false"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "hassio_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["esphome_list_devices"].annotations.readOnlyHint is True
            result = await session.call_tool("esphome_list_devices", {})
            assert not result.isError
            discovery = json.loads(result.content[0].text)
            assert discovery["items"][0]["device"] == "solder-table.yaml"
            assert discovery["items"][0]["status"] == "unknown"
            assert tools["esphome_get_config"].annotations.readOnlyHint is True
            result = await session.call_tool(
                "esphome_get_config", {"device": "solder-table"}
            )
            config = json.loads(result.content[0].text)
            assert config["success"] and "GPIO5 # relay" in config["content"]
            assert (
                config["sha256"]
                == hashlib.sha256(
                    (esphome_dir / "solder-table.yaml").read_bytes()
                ).hexdigest()
            )
            result = await session.call_tool(
                "esphome_get_config", {"device": "../secrets.yaml"}
            )
            assert (
                json.loads(result.content[0].text)["error"]["code"] == "ACCESS_DENIED"
            )
            result = await session.call_tool("esphome_list_devices", {"limit": 101})
            assert (
                json.loads(result.content[0].text)["error"]["code"] == "CONFIG_INVALID"
            )
            assert tools["get_version"].annotations.readOnlyHint is True
            result = await session.call_tool("get_version", {})
            assert not result.isError
            assert json.loads(result.content[0].text) == {
                "name": "Hassos MCP",
                "version": version("hassio-mcp"),
            }
            assert {
                "apply_automation_change",
                "apply_blueprint_change",
                "inspect_dependencies",
                "blueprint_consumers",
            } <= tools.keys()
            for name in (
                "enable_automation",
                "disable_automation",
                "trigger_automation",
                "reload_automations",
            ):
                assert tools[name].annotations.readOnlyHint is False
                assert "approved_digest" not in tools[name].inputSchema["properties"]
            assert tools["inspect_automation"].annotations.readOnlyHint is True
            assert tools["get_blueprint"].annotations.readOnlyHint is True
            assert tools["apply_blueprint_change"].annotations.destructiveHint is True
            assert tools["apply_blueprint_change"].annotations.readOnlyHint is False
            assert tools["submit_suggestion"].annotations.readOnlyHint is False
            assert tools["submit_suggestion"].annotations.idempotentHint is True
            assert tools["list_suggestions"].annotations.readOnlyHint is True
            assert tools["apply_automation_change"].annotations.destructiveHint is True
            assert tools["apply_automation_change"].annotations.readOnlyHint is False
            assert tools["get_state"].annotations.readOnlyHint is True
            result = await session.call_tool("get_state", {"entity_id": "sensor.test"})
            assert result.isError
            assert "HA_TOKEN is not configured" in result.content[0].text
            result = await session.call_tool(
                "apply_automation_change",
                {"proposal_id": "fake", "approved_digest": "fake"},
            )
            assert result.isError
            assert "disabled" in result.content[0].text
            result = await session.call_tool(
                "apply_blueprint_change",
                {"proposal_id": "fake", "approved_digest": "fake"},
            )
            assert result.isError and "disabled" in result.content[0].text
            result = await session.call_tool(
                "get_blueprint", {"domain": "automation", "path": "local/test.yaml"}
            )
            assert not result.isError
            assert json.loads(result.content[0].text)["yaml"] == blueprint.read_text()
            result = await session.call_tool(
                "propose_blueprint_change",
                {
                    "operation": "delete",
                    "domain": "automation",
                    "path": "local/test.yaml",
                },
            )
            assert result.isError and blueprint.exists()
            assert (
                "HA_TOKEN" in result.content[0].text
            )  # Impact inspection must succeed before proposing.
            result = await session.call_tool(
                "submit_suggestion",
                {
                    "kind": "feature_request",
                    "title": "Aligned history",
                    "details": "Return two series aligned to the same time buckets.",
                },
            )
            assert not result.isError
            saved = json.loads(result.content[0].text)
            assert saved["status"] == "saved"

    # A new MCP process sees the report, with no HA credentials or automation writes.
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_suggestions", {})
            assert not result.isError
            rows = json.loads(result.content[0].text)["items"]
            assert rows[0]["id"] == saved["id"]
            assert rows[0]["title"] == "Aligned history"
            assert blueprint.exists()
