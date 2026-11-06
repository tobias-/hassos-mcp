"""Reviewed native configuration CRUD for UI-managed scripts and scenes."""

import json
import re

from .dependencies import Dependencies
from .ha import HAError, identifier
from .yaml_files import dump, yaml_engine


class Resources:
    def __init__(self, ha, reviews):
        self.ha, self.reviews = ha, reviews
        self.dependencies = Dependencies(ha)

    def path(self, domain, resource_id):
        if domain not in ("script", "scene"):
            raise ValueError("Resource domain must be script or scene")
        identifier(resource_id)
        if domain == "script" and not re.fullmatch(r"[a-z0-9_]+", resource_id):
            raise ValueError("Script config IDs must be lowercase slugs")
        return f"/api/config/{domain}/config/{resource_id}"

    async def read(self, domain, resource_id):
        try:
            return await self.ha.request("GET", self.path(domain, resource_id))
        except HAError as exc:
            if exc.status == 404:
                return None
            raise

    async def impact(self, domain, resource_id):
        registry = await self.ha.ws("config/entity_registry/list")
        targets = sorted(
            entry["entity_id"]
            for entry in registry
            if entry.get("platform") in (domain, "homeassistant")
            and entry["entity_id"].startswith(domain + ".")
            and str(entry.get("unique_id")) == resource_id
        )
        return {
            "entities": targets,
            "dependencies": [
                await self.dependencies.related(domain, target) for target in targets
            ],
            "coverage": "Registered matching resources and HA's loaded dependency index; unloaded YAML and dynamic references may be absent.",
        }

    async def propose(self, domain, resource_id, operation, config_yaml=None):
        self.path(domain, resource_id)
        if operation not in ("create", "update", "delete"):
            raise ValueError("Operation must be create, update, or delete")
        before = await self.read(domain, resource_id)
        if (operation == "create") != (before is None):
            raise ValueError(
                "Create needs an absent UI-managed resource; update/delete need an existing one"
            )
        if operation == "delete":
            if config_yaml is not None:
                raise ValueError("Delete must omit config_yaml")
            after = None
        else:
            if config_yaml is None or len(config_yaml.encode()) > 128 * 1024:
                raise ValueError("Supply complete configuration YAML up to 128 KiB")
            after = yaml_engine().load(config_yaml)
            if not isinstance(after, dict):
                raise ValueError("Supply one resource mapping")
            try:
                after = json.loads(json.dumps(after, allow_nan=False))
            except (TypeError, ValueError):
                raise ValueError(
                    "Native configuration edits require JSON-compatible YAML without custom tags"
                ) from None
            if domain == "script" and not any(
                key in after for key in ("sequence", "use_blueprint")
            ):
                raise ValueError("Script needs sequence or use_blueprint")
            if domain == "scene":
                if (
                    str(after.get("id", resource_id)) != resource_id
                    or not isinstance(after.get("entities"), dict)
                    or not isinstance(after.get("name"), str)
                ):
                    raise ValueError(
                        "Scene needs matching id, name, and an entities mapping"
                    )
                after["id"] = resource_id
        if before == after:
            raise ValueError("No change proposed")
        impact = await self.impact(domain, resource_id)
        if operation == "create" and impact["entities"]:
            raise ValueError(
                "ID already belongs to a loaded YAML resource; use its allowlisted configuration file"
            )
        return self.reviews.propose(
            {
                "kind": "resource",
                "domain": domain,
                "resource_id": resource_id,
                "operation": operation,
                "before": before,
                "after": after,
                "impact": impact,
                "effects": "HA validates the resource, writes its UI-managed file, and schedules reload/removal. Runs may stop. Dependencies may be affected. YAML package resources require allowlisted file management.",
            }
        )

    async def apply(self, proposal_id, approved_digest):
        async def perform(payload):
            domain, resource_id = payload["domain"], payload["resource_id"]
            if (
                await self.read(domain, resource_id) != payload["before"]
                or await self.impact(domain, resource_id) != payload["impact"]
            ):
                raise ValueError(
                    "Resource or dependencies changed since review; propose again"
                )
            path = self.path(domain, resource_id)
            if payload["operation"] == "delete":
                await self.ha.request("DELETE", path)
            else:
                await self.ha.request("POST", path, body=payload["after"])
            if await self.read(domain, resource_id) != payload["after"]:
                raise RuntimeError(
                    "Saved resource differs; inspect status and current configuration before retrying"
                )
            return {
                "domain": domain,
                "resource_id": resource_id,
                "operation": payload["operation"],
                "note": "Stored configuration verified; inspect runtime state after HA finishes reloading.",
            }

        return await self.reviews.apply(
            proposal_id, approved_digest, "resource", perform
        )

    async def rollback(self, proposal_id):
        original = self.reviews.status(proposal_id)
        if original["kind"] != "resource" or original["status"] == "pending":
            raise ValueError("Select an applied or uncertain resource proposal")
        domain, resource_id = original["domain"], original["resource_id"]
        current = await self.read(domain, resource_id)
        before = original["before"]
        operation = (
            "delete" if before is None else "create" if current is None else "update"
        )
        return await self.propose(
            domain, resource_id, operation, None if before is None else dump(before)
        )
