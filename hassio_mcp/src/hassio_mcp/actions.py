"""Discoverable Home Assistant actions with explicit, single-use acceptance."""

import json
import re

from .dependencies import Dependencies
from .ha import HAError, entity
from .privacy import ensure_no_credentials


class Actions:
    def __init__(self, ha, reviews):
        self.ha, self.reviews = ha, reviews
        self.dependencies = Dependencies(ha)

    async def schema(self, domain, service):
        if not all(re.fullmatch(r"[a-z0-9_]+", value) for value in (domain, service)):
            raise ValueError("Use registered domain/service names")
        groups = await self.ha.request("GET", "/api/services")
        for group in groups:
            if group["domain"] == domain and service in group["services"]:
                return group["services"][service]
        raise ValueError("Service is not registered in Home Assistant")

    async def identity(self, targets):
        registry = await self.ha.ws("config/entity_registry/list")
        by_id = {row["entity_id"]: row for row in registry}
        states = {row["entity_id"] for row in await self.ha.states()}
        if any(target not in states for target in targets):
            raise ValueError(
                "A target has no current state; inspect availability before proposing"
            )
        return [
            {
                "entity_id": target,
                **{
                    key: by_id.get(target, {}).get(key)
                    for key in ("unique_id", "platform", "config_entry_id", "device_id")
                },
            }
            for target in targets
        ]

    async def propose(self, domain, service, entity_ids=None, data=None):
        if domain == "automation" and service in (
            "turn_on",
            "turn_off",
            "trigger",
            "reload",
        ):
            raise ValueError(
                "Use the dedicated preauthorized automation control tool; no proposal is needed"
            )
        targets = sorted(set(entity(item) for item in (entity_ids or [])))
        if len(targets) > 50:
            raise ValueError("Select at most 50 explicit entities")
        data = data or {}
        ensure_no_credentials(data, self.reviews.settings)
        if len(json.dumps(data, allow_nan=False).encode()) > 64 * 1024:
            raise ValueError("Action data exceeds 64 KiB")
        if any(
            key in data
            for key in (
                "entity_id",
                "device_id",
                "area_id",
                "floor_id",
                "label_id",
                "target",
            )
        ):
            raise ValueError(
                "Targets must be explicit entity_ids, not embedded in data"
            )
        schema = await self.schema(domain, service)
        if "target" in schema and schema["target"] is not None and not targets:
            raise ValueError("Select explicit entity_ids for a targeted service")
        unknown = data.keys() - schema.get("fields", {}).keys()
        if unknown:
            raise ValueError(
                "Unsupported service fields: " + ", ".join(sorted(unknown))
            )
        identity = await self.identity(targets) if targets else []
        impact = [
            await self.dependencies.related("entity", target) for target in targets
        ]
        return self.reviews.propose(
            {
                "kind": "action",
                "domain": domain,
                "service": service,
                "before": None,
                "after": {
                    "domain": domain,
                    "service": service,
                    "entity_ids": targets,
                    "data": data,
                },
                "schema": schema,
                "identity": identity,
                "impact": impact,
                "effects": "This performs a real HA action, possibly affecting devices, running scripts, activating scenes, sending messages, reloading components, or restarting HA. Actions may have irreversible effects and cannot be automatically rolled back. Review exact targets and arguments.",
            }
        )

    async def apply(self, proposal_id, approved_digest):
        async def perform(payload):
            domain, service = payload["domain"], payload["service"]
            targets, data = payload["after"]["entity_ids"], payload["after"]["data"]
            if await self.schema(domain, service) != payload["schema"]:
                raise ValueError("Service schema changed since review; propose again")
            if targets and await self.identity(targets) != payload["identity"]:
                raise ValueError("Target identity changed since review; propose again")
            if [
                await self.dependencies.related("entity", target) for target in targets
            ] != payload["impact"]:
                raise ValueError("Dependencies changed since review; propose again")
            body = {**data, **({"entity_id": targets} if targets else {})}
            params = (
                {"return_response": ""} if payload["schema"].get("response") else None
            )
            await self.ha.request(
                "POST", f"/api/services/{domain}/{service}", body=body, params=params
            )
            observed = []
            for target in targets:
                try:
                    observed.append(
                        await self.ha.request("GET", "/api/states/" + target)
                    )
                except (HAError, TimeoutError) as exc:
                    observed.append(
                        {
                            "entity_id": target,
                            "observation_error": str(exc) or "timeout",
                        }
                    )
            return {
                "domain": domain,
                "service": service,
                "observed_states": observed,
                "note": "HA completed the service request; observed states do not prove physical success or completion of asynchronous scripts. Never automatically repeat an action after uncertainty. There is no automatic action rollback.",
            }

        return await self.reviews.apply(proposal_id, approved_digest, "action", perform)
