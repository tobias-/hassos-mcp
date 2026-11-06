"""Narrow automation controls authorized independently of configuration edits."""

import asyncio

from .ha import HAError, entity, identifier


class AutomationManagement:
    def __init__(self, ha):
        self.ha = ha

    async def inspect(self, entity_id, include_traces=True):
        target = entity(entity_id, "automation")
        state, config = await asyncio.gather(
            self.ha.request("GET", "/api/states/" + target),
            self.ha.ws("automation/config", entity_id=target),
        )
        result = {"entity_id": target, "state": state, "configuration": config}
        if include_traces:
            config_body = config.get("config", config)
            automation_id = state.get("attributes", {}).get("id") or config_body.get(
                "id"
            )
            if automation_id is None:
                result["trace_note"] = (
                    "No automation ID is available; saved traces require an ID."
                )
            else:
                try:
                    result["traces"] = await self.ha.ws(
                        "trace/list",
                        domain="automation",
                        item_id=identifier(str(automation_id)),
                    )
                except (HAError, ValueError, TimeoutError) as exc:
                    result["trace_note"] = str(exc) or "Trace lookup timed out"
        return result

    async def control(
        self, operation, entity_id=None, *, stop_actions=True, skip_condition=False
    ):
        services = {
            "enable": "turn_on",
            "disable": "turn_off",
            "trigger": "trigger",
            "reload": "reload",
        }
        if operation not in services:
            raise ValueError("Unsupported automation operation")
        if operation == "reload":
            if entity_id is not None:
                raise ValueError("Reload applies to all automations; omit entity_id")
            body = {}
        else:
            target = entity(entity_id, "automation")
            # Require a real, specific target rather than accepting a silent service no-op.
            state = await self.ha.request("GET", "/api/states/" + target)
            if state.get("entity_id") != target:
                raise ValueError("Automation entity was not found")
            body = {"entity_id": target}
            if operation == "disable":
                body["stop_actions"] = stop_actions
            elif operation == "trigger":
                body["skip_condition"] = skip_condition
        try:
            await self.ha.request(
                "POST", "/api/services/automation/" + services[operation], body=body
            )
        except (HAError, TimeoutError) as exc:
            raise RuntimeError(
                f"Automation {operation} failed or its outcome is uncertain. It may already have run; inspect state/traces before retrying. {exc}"
            ) from None
        result = {
            "operation": operation,
            "status": "request_completed",
            "parameters": body,
            "approval_required": False,
            "note": "HA completed the service request. Inspect state/traces to verify effects; this does not prove an action run succeeded. Do not automatically retry a trigger.",
        }
        if operation in ("enable", "disable"):
            try:
                result["state"] = await self.ha.request("GET", "/api/states/" + target)
            except (HAError, TimeoutError) as exc:
                result["state_read_error"] = str(exc) or "State lookup timed out"
        return result
