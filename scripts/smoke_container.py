"""Verify the built images over actual MCP stdio, without network or credentials."""

import asyncio
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(images):
    with TemporaryDirectory(prefix="hassio-mcp-esphome-") as directory:
        await check_images(images, Path(directory))


async def check_images(images, directory):
    directory.chmod(0o755)  # Compose image runs as the unprivileged MCP user.
    source = (
        "# Offline controller fixture\nesphome:\n  name: solder-table\n"
        "esp8266:\n  board: nodemcuv2\nwifi:\n  password: !secret wifi_password\n"
        "switch:\n  - platform: gpio\n    pin: GPIO5 # original relay\n"
    )
    config = directory / "solder-table.yaml"
    config.write_text(source)
    config.chmod(0o444)
    private = directory / "private.yaml"
    private.write_text(source.replace("!secret wifi_password", "private-test-password"))
    private.chmod(0o444)
    project = Path(__file__).resolve().parents[1] / "hassio_mcp/pyproject.toml"
    expected_version = tomllib.loads(project.read_text())["project"]["version"]
    for image in images:
        params = StdioServerParameters(
            command="docker",
            args=[
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--entrypoint",
                "hassio-mcp",
                "--mount",
                f"type=bind,src={directory},dst=/esphome-config,readonly",
                "-e",
                "ESPHOME_CONFIG_DIR=/esphome-config",
                image,
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.version == expected_version
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["get_version"].annotations.readOnlyHint is True
            result = await session.call_tool("get_version", {})
            assert not result.isError
            assert json.loads(result.content[0].text)["version"] == expected_version
            for name in ("esphome_list_devices", "esphome_get_config"):
                assert tools[name].annotations.readOnlyHint is True
                assert tools[name].annotations.destructiveHint is False
            result = await session.call_tool("esphome_list_devices", {})
            discovery = json.loads(result.content[0].text)
            assert discovery["success"]
            controller = next(
                row
                for row in discovery["items"]
                if row["device"] == "solder-table.yaml"
            )
            assert (
                controller["status"] == "unknown" and controller["board"] == "nodemcuv2"
            )
            result = await session.call_tool(
                "esphome_get_config", {"device": "solder-table"}
            )
            read = json.loads(result.content[0].text)
            assert read["success"] and read["content"] == source
            assert read["sha256"] == hashlib.sha256(source.encode()).hexdigest()
            assert read["redacted"] is read["truncated"] is False
            result = await session.call_tool(
                "esphome_get_config", {"device": "private.yaml"}
            )
            assert (
                json.loads(result.content[0].text)["error"]["code"] == "ACCESS_DENIED"
            )
            assert "private-test-password" not in result.content[0].text
            assert {
                "esphome_get_status",
                "esphome_get_logs",
                "esphome_validate",
                "esphome_propose_config_change",
                "esphome_apply_config_change",
                "esphome_compile",
                "esphome_propose_install",
                "esphome_install",
                "esphome_operation_status",
                "esphome_change_status",
                "get_config_file",
                "propose_config_file_change",
                "apply_config_file_change",
                "apply_resource_change",
                "apply_service_action",
                "discover_home_assistant",
                "propose_blueprint_rollback",
            } <= tools.keys()
            result = await session.call_tool(
                "esphome_install",
                {
                    "device": "solder-table",
                    "artifact_id": "a" * 32,
                    "firmware_sha256": "b" * 64,
                    "proposal_id": "test",
                    "approved_digest": "test",
                },
            )
            assert (
                json.loads(result.content[0].text)["error"]["code"] == "ACCESS_DENIED"
            )
            result = await session.call_tool(
                "esphome_apply_config_change",
                {"proposal_id": "test", "approved_digest": "test"},
            )
            assert (
                json.loads(result.content[0].text)["error"]["code"] == "ACCESS_DENIED"
            )
            for name in (
                "apply_resource_change",
                "apply_service_action",
                "apply_config_file_change",
            ):
                assert tools[name].annotations.readOnlyHint is False
                result = await session.call_tool(
                    name, {"proposal_id": "test", "approved_digest": "test"}
                )
                assert result.isError and "disabled" in result.content[0].text
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
            assert tools["apply_blueprint_change"].annotations.destructiveHint
            assert tools["apply_automation_change"].annotations.destructiveHint
            result = await session.call_tool(
                "apply_automation_change",
                {"proposal_id": "test", "approved_digest": "test"},
            )
            assert result.isError and "disabled" in result.content[0].text
            result = await session.call_tool(
                "apply_blueprint_change",
                {"proposal_id": "test", "approved_digest": "test"},
            )
            assert result.isError and "disabled" in result.content[0].text
            result = await session.call_tool(
                "submit_suggestion",
                {
                    "kind": "bug",
                    "title": "Smoke test",
                    "details": "Temporary container persistence check",
                },
            )
            assert not result.isError
            result = await session.call_tool("list_suggestions", {})
            assert not result.isError and "Smoke test" in result.content[0].text
            print(
                f"{image}: version {expected_version}, offline ESPHome discovery/reads, credential rejection, MCP discovery, suggestion box, and disabled-write guards passed"
            )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: smoke_container.py IMAGE [IMAGE ...]")
    asyncio.run(main(sys.argv[1:]))
