import copy

import pytest
from hassio_mcp.blueprints import Blueprints, validate_consumer_inputs
from hassio_mcp.config import Settings
from hassio_mcp.dependencies import Dependencies
from hassio_mcp.ha import HAError

SOURCE = "blueprint:\n  name: Motion\n  domain: automation\n  input:\n    lamp: {}\ntriggers: []\nactions:\n- action: light.turn_on\n  target:\n    entity_id: !input lamp\n"


class HA:
    def __init__(self):
        self.ref = {"path": "local/motion.yaml", "input": {"lamp": "light.test"}}
        self.writes = []
        self.saved_available = True
        self.domain = "automation"
        self.config_id = "original_id"
        self.runtime = {"id": self.config_id, "triggers": [], "actions": []}
        self.reads = []

    async def ws(self, command, **params):
        if command == "search/related":
            assert params["include_disabled_entities"] is True
            return {self.domain: [self.domain + ".motion"]}
        if command in ("automation/config", "script/config"):
            # HA returns expanded runtime config without use_blueprint.
            return {"config": copy.deepcopy(self.runtime)}
        if command == "config/entity_registry/get":
            return {"entity_id": "script.motion", "unique_id": self.config_id}
        self.writes.append(command)

    async def request(self, method, path):
        assert method == "GET"
        self.reads.append(path)
        if path.startswith("/api/states/"):
            return {"attributes": {"id": self.config_id}}
        assert path == f"/api/config/{self.domain}/config/{self.config_id}"
        if not self.saved_available:
            raise HAError(404)
        return {"use_blueprint": copy.deepcopy(self.ref)}


async def test_blueprint_inputs_returned_without_detaching_and_stale_dependencies_block(
    tmp_path,
):
    ha = HA()
    report = await Dependencies(ha).blueprint_consumers(
        "automation", "local/motion.yaml"
    )
    assert report["consumers"][0]["use_blueprint"] == ha.ref
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        blueprint_writes_enabled=True,
    )
    path = settings.config_dir / "blueprints/automation/local/motion.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE)
    manager = Blueprints(settings, ha)
    proposal = await manager.propose(
        "update",
        "automation",
        "local/motion.yaml",
        SOURCE.replace("name: Motion", "name: Updated"),
    )
    assert proposal["impact"] == report
    ha.ref["input"]["lamp"] = "light.changed"
    with pytest.raises(ValueError, match="consumers or inputs changed"):
        await manager.apply(proposal["proposal_id"], proposal["digest"])
    assert not ha.writes and path.read_text() == SOURCE


@pytest.mark.parametrize("domain", ["automation", "script"])
async def test_expanded_config_resolves_original_inputs_and_config_id(domain):
    ha = HA()
    ha.domain = domain
    ha.runtime = {"sequence": []} if domain == "script" else {"actions": []}
    report = await Dependencies(ha).blueprint_consumers(domain, "local/motion.yaml")
    assert report["consumers"] == [
        {"entity_id": domain + ".motion", "use_blueprint": ha.ref}
    ]
    assert f"/api/config/{domain}/config/original_id" in ha.reads
    assert not ha.writes


async def test_runtime_config_with_original_reference_needs_no_fallback():
    ha = HA()
    ha.runtime = {"use_blueprint": ha.ref}
    await Dependencies(ha).blueprint_consumers("automation", "local/motion.yaml")
    assert not ha.reads


@pytest.mark.parametrize("reference", [None, {"path": "other.yaml"}])
async def test_genuine_saved_reference_mismatch_still_blocks(reference):
    ha = HA()
    ha.ref = reference
    with pytest.raises(ValueError, match="Saved blueprint reference.*differs"):
        await Dependencies(ha).blueprint_consumers("automation", "local/motion.yaml")


async def test_allowlisted_yaml_original_inputs_and_missing_source(tmp_path):
    ha = HA()
    ha.saved_available = False
    settings = Settings(config_dir=tmp_path, yaml_files=("package.yaml",))
    (tmp_path / "package.yaml").write_text(
        "automation:\n- id: original_id\n  use_blueprint:\n"
        "    path: local/motion.yaml\n    input:\n      lamp: light.test\n"
    )
    result = await Dependencies(ha, settings).blueprint_consumers(
        "automation", "local/motion.yaml"
    )
    assert result["consumers"][0]["use_blueprint"] == ha.ref
    settings.yaml_files = ()
    with pytest.raises(ValueError, match="add the source file to yaml_files"):
        await Dependencies(ha, settings).blueprint_consumers(
            "automation", "local/motion.yaml"
        )


async def test_failed_saved_config_read_is_not_hidden():
    ha = HA()

    async def denied(*args):
        raise HAError(403)

    ha.request = denied
    with pytest.raises(HAError) as error:
        await Dependencies(ha).blueprint_consumers("automation", "local/motion.yaml")
    assert error.value.status == 403


def test_inputs_and_sections_validated_before_apply():
    consumers = [
        {
            "entity_id": "automation.test",
            "use_blueprint": {
                "path": "local/motion.yaml",
                "input": {"lamp": "light.test"},
            },
        }
    ]
    validate_consumer_inputs(SOURCE, "automation", consumers)
    with pytest.raises(ValueError, match="Undefined"):
        validate_consumer_inputs(
            SOURCE.replace("!input lamp", "!input missing"), "automation", consumers
        )
    with pytest.raises(ValueError, match="lacks inputs"):
        validate_consumer_inputs(
            SOURCE.replace("    lamp: {}", "    lamp: {}\n    added: {}"),
            "automation",
            consumers,
        )
    validate_consumer_inputs(
        SOURCE.replace(
            "    lamp: {}",
            "    section:\n      input:\n        lamp: {}\n        added:\n          default: 2",
        ),
        "automation",
        consumers,
    )


async def test_template_dependencies_report_unknown():
    result = await Dependencies(HA()).blueprint_consumers("template", "local/test.yaml")
    assert "unknown" in result["coverage"]
