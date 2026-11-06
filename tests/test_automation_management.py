import copy

import pytest
from hassio_mcp.automation_management import AutomationManagement
from hassio_mcp.config import Settings
from hassio_mcp.ha import HAError
from hassio_mcp.server import build_server


class FakeHA:
    def __init__(self):
        self.calls = []
        self.state = {
            "entity_id": "automation.test",
            "state": "off",
            "attributes": {"id": "config_id", "friendly_name": "Test", "current": 0},
        }
        self.fail_post = False
        self.fail_trace = False

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            return copy.deepcopy(self.state)
        if self.fail_post:
            raise HAError("transport")
        if path.endswith("turn_on"):
            self.state["state"] = "on"
        elif path.endswith("turn_off"):
            self.state["state"] = "off"
        return []

    async def ws(self, command, **params):
        self.calls.append(("WS", command, params))
        if command == "automation/config":
            return {
                "config": {
                    "id": "config_id",
                    "alias": "Test",
                    "triggers": [],
                    "actions": [],
                }
            }
        if self.fail_trace:
            raise HAError("not_found")
        return [{"run_id": "stored-run"}]

    async def states(self):
        return [copy.deepcopy(self.state), {"entity_id": "light.test", "state": "on"}]


@pytest.mark.parametrize(
    "tool,params,service,body",
    [
        (
            "enable_automation",
            {"entity_id": "automation.test"},
            "turn_on",
            {"entity_id": "automation.test"},
        ),
        (
            "disable_automation",
            {"entity_id": "automation.test"},
            "turn_off",
            {"entity_id": "automation.test", "stop_actions": True},
        ),
        (
            "disable_automation",
            {"entity_id": "automation.test", "stop_actions": False},
            "turn_off",
            {"entity_id": "automation.test", "stop_actions": False},
        ),
        (
            "trigger_automation",
            {"entity_id": "automation.test"},
            "trigger",
            {"entity_id": "automation.test", "skip_condition": False},
        ),
        (
            "trigger_automation",
            {"entity_id": "automation.test", "skip_condition": True},
            "trigger",
            {"entity_id": "automation.test", "skip_condition": True},
        ),
        ("reload_automations", {}, "reload", {}),
    ],
)
async def test_direct_tools_work_with_configuration_writes_disabled(
    tmp_path, tool, params, service, body
):
    ha = FakeHA()
    server = build_server(
        Settings(
            data_dir=tmp_path, writes_enabled=False, blueprint_writes_enabled=False
        ),
        ha,
    )
    result = await server.call_tool(tool, params)
    assert result
    assert [call for call in ha.calls if call[0] == "POST"] == [
        ("POST", "/api/services/automation/" + service, {"body": body})
    ]
    tools = {item.name: item for item in await server.list_tools()}
    assert tools[tool].annotations.readOnlyHint is False
    assert not tools[tool].annotations.idempotentHint
    assert "proposal_id" not in tools[tool].inputSchema["properties"]
    assert "approved_digest" not in tools[tool].inputSchema["properties"]


async def test_inspect_combines_configuration_state_and_trace_without_writes():
    ha = FakeHA()
    result = await AutomationManagement(ha).inspect("automation.test")
    assert result["state"]["state"] == "off"
    assert result["configuration"]["config"]["id"] == "config_id"
    assert result["traces"] == [{"run_id": "stored-run"}]
    assert (
        "WS",
        "trace/list",
        {"domain": "automation", "item_id": "config_id"},
    ) in ha.calls
    assert not any(call[0] == "POST" for call in ha.calls)
    ha.fail_trace = True
    assert "trace_note" in await AutomationManagement(ha).inspect("automation.test")
    ha.calls.clear()
    assert "traces" not in await AutomationManagement(ha).inspect(
        "automation.test", False
    )
    assert not any(call[1] == "trace/list" for call in ha.calls)


@pytest.mark.parametrize(
    "target",
    [
        "all",
        "automation.*",
        "light.test",
        "automation.test,automation.other",
        "../automation.test",
    ],
)
async def test_control_rejects_broad_or_non_automation_targets(target):
    ha = FakeHA()
    with pytest.raises(ValueError):
        await AutomationManagement(ha).control("trigger", target)
    assert not ha.calls


async def test_unknown_entity_or_operation_never_posts():
    ha = FakeHA()
    manager = AutomationManagement(ha)
    with pytest.raises(ValueError, match="not found"):
        await manager.control("trigger", "automation.other")
    with pytest.raises(ValueError, match="Unsupported"):
        await manager.control("delete", "automation.test")
    with pytest.raises(ValueError, match="all automations"):
        await manager.control("reload", "automation.test")
    assert not any(call[0] == "POST" for call in ha.calls)


async def test_uncertain_trigger_is_never_retried():
    ha = FakeHA()
    ha.fail_post = True
    with pytest.raises(RuntimeError, match="may already have run"):
        await AutomationManagement(ha).control("trigger", "automation.test")
    assert len([call for call in ha.calls if call[0] == "POST"]) == 1


async def test_list_filters_other_domains_and_inspect_tool_is_readonly(tmp_path):
    ha = FakeHA()
    server = build_server(Settings(data_dir=tmp_path), ha)
    result = await server.call_tool("list_automations", {})
    assert "automation.test" in str(result)
    assert "light.test" not in str(result)
    assert await server.call_tool(
        "inspect_automation", {"entity_id": "automation.test"}
    )
    tools = {item.name: item for item in await server.list_tools()}
    assert tools["inspect_automation"].annotations.readOnlyHint
    assert not any(call[0] == "POST" for call in ha.calls)
