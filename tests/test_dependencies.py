import copy

import pytest
from hassio_mcp.blueprints import Blueprints, validate_consumer_inputs
from hassio_mcp.config import Settings
from hassio_mcp.dependencies import Dependencies

SOURCE = "blueprint:\n  name: Motion\n  domain: automation\n  input:\n    lamp: {}\ntriggers: []\nactions:\n- action: light.turn_on\n  target:\n    entity_id: !input lamp\n"


class HA:
    def __init__(self):
        self.ref = {"path": "local/motion.yaml", "input": {"lamp": "light.test"}}
        self.writes = []

    async def ws(self, command, **params):
        if command == "search/related":
            assert params["include_disabled_entities"] is True
            return {"automation": ["automation.motion"]}
        if command == "automation/config":
            return {"config": {"use_blueprint": copy.deepcopy(self.ref)}}
        self.writes.append(command)


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
