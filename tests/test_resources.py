import copy

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.ha import HAError
from hassio_mcp.resources import Resources
from hassio_mcp.review import ReviewStore


class HA:
    def __init__(self):
        self.value = None
        self.writes = []
        self.fail = False

    async def request(self, method, path, **kwargs):
        if method == "GET":
            if self.value is None:
                raise HAError(404)
            return copy.deepcopy(self.value)
        self.writes.append((method, path, kwargs))
        if self.fail:
            raise HAError("transport")
        self.value = None if method == "DELETE" else copy.deepcopy(kwargs["body"])
        return {"result": "ok"}

    async def ws(self, command, **kwargs):
        return [] if command == "config/entity_registry/list" else {}


@pytest.mark.parametrize(
    "domain,config",
    [
        ("script", "alias: Test\nsequence: []\n"),
        ("scene", "name: Test\nentities: {}\n"),
    ],
)
async def test_resource_crud_and_rollback_need_exact_acceptance(
    tmp_path, domain, config
):
    settings = Settings(data_dir=tmp_path, management_writes_enabled=True)
    reviews, ha = ReviewStore(settings), HA()
    manager = Resources(ha, reviews)
    proposal = await manager.propose(domain, "test", "create", config)
    assert not ha.writes
    with pytest.raises(ValueError, match="digest"):
        await manager.apply(proposal["proposal_id"], "wrong")
    await manager.apply(proposal["proposal_id"], proposal["digest"])
    assert ha.value is not None
    with pytest.raises(ValueError, match="consumed"):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    restore = await manager.rollback(proposal["proposal_id"])
    assert restore["operation"] == "delete" and ha.value is not None
    await manager.apply(restore["proposal_id"], restore["digest"])
    assert ha.value is None
    assert ReviewStore(settings).status(proposal["proposal_id"])["status"] == "applied"


async def test_disabled_stale_and_uncertain_writes(tmp_path):
    settings = Settings(data_dir=tmp_path)
    reviews, ha = ReviewStore(settings), HA()
    manager = Resources(ha, reviews)
    proposal = await manager.propose("script", "test", "create", "sequence: []")
    with pytest.raises(ValueError, match="disabled"):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    settings.management_writes_enabled = True
    ha.value = {"sequence": []}
    with pytest.raises(ValueError, match="changed"):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    assert not ha.writes
    proposal = await manager.propose("script", "test", "delete")
    ha.fail = True
    with pytest.raises(HAError):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    assert reviews.status(proposal["proposal_id"])["status"] == "failed_or_uncertain"
    with pytest.raises(ValueError, match="consumed"):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    assert len(ha.writes) == 1
