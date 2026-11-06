"""Read registry metadata without integration credentials or config-entry data."""

REGISTRIES = {
    "entities": (
        "config/entity_registry/list",
        (
            "entity_id",
            "unique_id",
            "platform",
            "config_entry_id",
            "device_id",
            "area_id",
            "name",
            "original_name",
            "disabled_by",
            "hidden_by",
            "capabilities",
        ),
    ),
    "devices": (
        "config/device_registry/list",
        (
            "id",
            "name",
            "name_by_user",
            "manufacturer",
            "model",
            "area_id",
            "config_entries",
            "disabled_by",
            "via_device_id",
        ),
    ),
    "areas": ("config/area_registry/list", ("area_id", "name", "floor_id", "aliases")),
    "integrations": (
        "config_entries/get",
        (
            "entry_id",
            "domain",
            "title",
            "state",
            "disabled_by",
            "source",
            "supports_unload",
            "supports_reconfigure",
        ),
    ),
}


async def discover(ha, kind):
    if kind == "services":
        return await ha.request("GET", "/api/services")
    if kind not in REGISTRIES:
        raise ValueError("Select entities, devices, areas, integrations, or services")
    command, fields = REGISTRIES[kind]
    rows = await ha.ws(command)
    return [{key: row[key] for key in fields if key in row} for row in rows]
