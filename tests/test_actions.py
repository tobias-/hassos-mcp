import copy

import pytest
from hassio_mcp.actions import Actions
from hassio_mcp.config import Settings
from hassio_mcp.ha import HAError
from hassio_mcp.review import ReviewStore


class HA:
    def __init__(self):
        self.writes = []
        self.unique_id = "one"
        self.fail = False

    async def request(self, method, path, **kwargs):
        if path == "/api/services":
            return [
                {"domain": domain, "services": {service: schema}}
                for domain, service, schema in (
                    ("light", "turn_on", {"target": {}, "fields": {"brightness": {}}}),
                    ("script", "turn_on", {"target": {}, "fields": {"variables": {}}}),
                    ("scene", "turn_on", {"target": {}, "fields": {}}),
                    ("script", "reload", {"fields": {}}),
                    ("homeassistant", "restart", {"fields": {}}),
                )
            ]
        if method == "POST":
            self.writes.append((path, copy.deepcopy(kwargs)))
            if self.fail:
                raise HAError("transport")
            return []
        return {"entity_id": path.rsplit("/", 1)[-1], "state": "on"}

    async def states(self):
        return [
            {"entity_id": value, "state": "off"}
            for value in ("light.test", "script.test", "scene.test")
        ]

    async def ws(self, command, **params):
        if command == "config/entity_registry/list":
            return [{"entity_id": "light.test", "unique_id": self.unique_id}]
        return {}


@pytest.mark.parametrize(
    "domain,service,targets,data",
    [
        ("light", "turn_on", ["light.test"], {"brightness": 100}),
        ("script", "turn_on", ["script.test"], {"variables": {"count": 2}}),
        ("scene", "turn_on", ["scene.test"], {}),
        ("script", "reload", [], {}),
        ("homeassistant", "restart", [], {}),
    ],
)
async def test_actions_require_exact_single_use_acceptance(
    tmp_path, domain, service, targets, data
):
    ha = HA()
    settings = Settings(data_dir=tmp_path, management_writes_enabled=True)
    reviews = ReviewStore(settings)
    manager = Actions(ha, reviews)
    proposed = await manager.propose(domain, service, targets, data)
    assert not ha.writes and proposed["approval_required"]
    with pytest.raises(ValueError, match="digest"):
        await manager.apply(proposed["proposal_id"], "wrong")
    await manager.apply(proposed["proposal_id"], proposed["digest"])
    assert ha.writes[0][0] == f"/api/services/{domain}/{service}"
    assert ha.writes[0][1]["body"] == {
        **data,
        **({"entity_id": targets} if targets else {}),
    }
    with pytest.raises(ValueError, match="consumed"):
        await manager.apply(proposed["proposal_id"], proposed["digest"])
    assert len(ha.writes) == 1


async def test_action_scope_staleness_and_uncertain_failure(tmp_path):
    ha = HA()
    reviews = ReviewStore(Settings(data_dir=tmp_path, management_writes_enabled=True))
    manager = Actions(ha, reviews)
    for args in [
        ("automation", "trigger", ["automation.test"], {}),
        ("light", "turn_on", [], {}),
        ("light", "turn_on", ["light.test"], {"entity_id": "all"}),
        ("light", "turn_on", ["light.test"], {"unsupported": 1}),
    ]:
        with pytest.raises(ValueError):
            await manager.propose(*args)
    proposed = await manager.propose("light", "turn_on", ["light.test"])
    ha.unique_id = "replacement-device"
    with pytest.raises(ValueError, match="identity changed"):
        await manager.apply(proposed["proposal_id"], proposed["digest"])
    assert not ha.writes
    proposed = await manager.propose("light", "turn_on", ["light.test"])
    ha.fail = True
    with pytest.raises(HAError):
        await manager.apply(proposed["proposal_id"], proposed["digest"])
    assert reviews.status(proposed["proposal_id"])["status"] == "failed_or_uncertain"
    with pytest.raises(ValueError, match="consumed"):
        await manager.apply(proposed["proposal_id"], proposed["digest"])
    assert len(ha.writes) == 1
