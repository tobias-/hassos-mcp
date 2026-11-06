"""Read HA's dependency index and preserve exact blueprint consumer references."""

import asyncio
import json

from .ha import HAError, entity, identifier
from .yaml_files import AutomationFiles

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
    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings or getattr(ha, "settings", None)

    async def consumer_config(self, domain, target):
        """Resolve saved inputs, which HA omits from expanded runtime config."""
        target = entity(target, domain)
        result = await self.ha.ws(domain + "/config", entity_id=target)
        config = result.get("config", result)
        if isinstance(config, dict) and "use_blueprint" in config:
            return config

        if domain == "automation":
            resource_id = config.get("id") if isinstance(config, dict) else None
            if resource_id is None:
                state = await self.ha.request("GET", "/api/states/" + target)
                resource_id = state.get("attributes", {}).get("id")
        else:
            # Entity IDs can be renamed independently of the script's config key.
            entry = await self.ha.ws("config/entity_registry/get", entity_id=target)
            resource_id = entry.get("unique_id") if entry else None

        if resource_id is None:
            raise ValueError(
                f"Cannot resolve the saved configuration ID for {target}; "
                "expanded runtime configuration does not contain blueprint inputs"
            )
        resource_id = identifier(str(resource_id))
        try:
            saved = await self.ha.request(
                "GET", f"/api/config/{domain}/config/{resource_id}"
            )
        except HAError as exc:
            if exc.status != 404:
                raise
            saved = None
        if saved is not None:
            return saved

        if domain == "automation" and self.settings:
            files = AutomationFiles(self.settings)
            matches = []
            for name in self.settings.yaml_files:
                _, _, items = files.load(name)
                matches.extend(
                    item for item in items if str(item.get("id")) == resource_id
                )
            if len(matches) > 1:
                raise ValueError(f"Ambiguous saved automation ID for {target}")
            if matches:
                return matches[0]
        raise ValueError(
            f"Saved blueprint inputs for {target} are unavailable through HA's "
            "UI configuration API. For YAML automations, mount the HA configuration "
            "directory and add the source file to yaml_files (HA_YAML_FILES in Compose). "
            "YAML-only script inputs cannot currently be resolved. "
            "Blueprint changes remain blocked until the original inputs can be read."
        )

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
        related = {key: sorted(values) for key, values in related.items()}
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
                config = await self.consumer_config(domain, target)
            reference = (
                config.get("use_blueprint") if isinstance(config, dict) else None
            )
            if not isinstance(reference, dict) or reference.get("path") != path:
                raise ValueError(
                    f"Saved blueprint reference for {target} differs from HA's "
                    "loaded dependency index; inspect the saved configuration and "
                    "reload the affected domain before retrying"
                )
            try:
                reference = json.loads(json.dumps(reference, allow_nan=False))
            except (TypeError, ValueError):
                raise ValueError(
                    f"Blueprint inputs for {target} contain unresolved YAML tags "
                    "or non-JSON values; exact inputs cannot be verified"
                ) from None
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
