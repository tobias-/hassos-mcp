"""Verify the built images over actual MCP stdio, without network or credentials."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(images):
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
                image,
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 17
            assert tools["apply_automation_change"].annotations.destructiveHint
            result = await session.call_tool(
                "apply_automation_change",
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
                f"{image}: MCP initialization, discovery, suggestion box, and disabled-write guard passed"
            )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: smoke_container.py IMAGE [IMAGE ...]")
    asyncio.run(main(sys.argv[1:]))
