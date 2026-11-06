import json
import os
import sys

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
    env["ALLOW_BLUEPRINT_WRITES"] = "false"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "hassio_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 22
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
            assert not result.isError and blueprint.exists()
            blueprint_proposal = json.loads(result.content[0].text)
            assert blueprint_proposal["approval_required"] is True
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
            result = await session.call_tool(
                "blueprint_change_status",
                {"proposal_id": blueprint_proposal["proposal_id"]},
            )
            assert json.loads(result.content[0].text)["status"] == "pending"
            assert blueprint.exists()
