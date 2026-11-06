import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_discovery_and_missing_credentials(tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in ("HA_TOKEN", "SUPERVISOR_TOKEN")
    }
    env["DATA_DIR"] = str(tmp_path)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "hassio_mcp.server"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 15
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
