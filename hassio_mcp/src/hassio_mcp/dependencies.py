"""Read HA's dependency index and preserve exact blueprint consumer references."""

import asyncio

from .ha import entity

ITEM_TYPES = (
    "entity",
    "automation",
    "script",
    "scene",
    "device",
    "area",
    "floor",
    "label",
    "integration",
    "config_entry",
    "automation_blueprint",
    "script_blueprint",
)


class Dependencies:
    def __init__(self, ha):
        self.ha = ha

    async def related(self, item_type, item_id):
        if item_type not in ITEM_TYPES or not item_id or len(item_id) > 240:
            raise ValueError(
                "Select a supported item type and a nonempty ID up to 240 characters"
            )
        related = await self.ha.ws(
            "search/related",
            item_type=item_type,
            item_id=item_id,
            include_disabled_entities=True,
        )
        return {
            "item_type": item_type,
            "item_id": item_id,
            "related": related,
            "coverage": "HA's loaded dependency index. Dynamic templates, external callers, and unloaded YAML may be absent; related items are not necessarily directional dependents.",
        }

    async def blueprint_consumers(self, domain, path):
        if domain == "template":
            return {
                "consumers": [],
                "coverage": "HA exposes no template-blueprint dependency index; dependencies are unknown, not absent.",
            }
        result = await self.related(domain + "_blueprint", path)
        ids = sorted(result["related"].get(domain, []))
        if len(ids) > 500:
            raise ValueError(
                "More than 500 blueprint consumers; inspect outside this tool before changing"
            )
        semaphore = asyncio.Semaphore(8)

        async def inspect(target):
            async with semaphore:
                config = await self.ha.ws(
                    domain + "/config", entity_id=entity(target, domain)
                )
            config = config.get("config", config)
            reference = config.get("use_blueprint")
            if not isinstance(reference, dict) or reference.get("path") != path:
                raise ValueError(
                    "Blueprint dependency index changed; inspect and retry the proposal"
                )
            return {"entity_id": target, "use_blueprint": reference}

        return {
            "consumers": await asyncio.gather(*(inspect(target) for target in ids)),
            "coverage": result["coverage"],
        }

    async def automation_impact(self, automation_id):
        targets = sorted(
            state["entity_id"]
            for state in await self.ha.states()
            if state["entity_id"].startswith("automation.")
            and str(state.get("attributes", {}).get("id")) == automation_id
        )
        return {
            "entities": targets,
            "dependencies": [
                await self.related("automation", target) for target in targets
            ],
            "note": "Only loaded automations can be resolved to entity IDs; empty results do not establish absence of external references.",
        }
