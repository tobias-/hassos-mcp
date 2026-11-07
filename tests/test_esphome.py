import json
import os

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.esphome import MAX_CONFIG_BYTES, ESPHome

SOURCE = """# Preserve the original relay wiring.
esphome:
  name: solder-table
  friendly_name: Soldering station
esp8266:
  board: nodemcuv2
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
api:
  encryption:
    key: !secret solder_api_key
ota:
  - platform: esphome
    password: !secret ota_password
switch:
  - platform: gpio
    id: heater_relay
    pin: GPIO5 # Relay, active low
    inverted: true
"""


@pytest.fixture
def store(tmp_path):
    settings = Settings(config_dir=tmp_path, data_dir=tmp_path / "data")
    root = tmp_path / "esphome"
    root.mkdir()
    (root / "solder-table.yaml").write_text(SOURCE)
    return ESPHome(settings)


def test_discovers_offline_controller_without_runtime_or_network(store):
    result = store.list_devices()
    assert result["success"] and not result["skipped"]
    record = result["items"][0]
    assert record["device"] == "solder-table.yaml"
    assert record["name"] == "solder-table"
    assert record["friendly_name"] == "Soldering station"
    assert record["platform"] == "esp8266"
    assert record["board"] == "nodemcuv2"
    assert record["config_present"] is True
    assert record["last_modified"].endswith("+00:00")
    assert record["status"] == "unknown"
    assert (
        record["hostname"]
        is record["address"]
        is record["home_assistant_registered"]
        is None
    )
    assert (store.root / "solder-table.yaml").read_text() == SOURCE


def test_pagination_covers_skipped_files_and_is_sorted(store):
    (store.root / "esphome-67.yml").write_text(SOURCE)
    (store.root / "esphome-50.yaml").write_text(SOURCE)
    (store.root / "package.yaml").write_text("switch: []\n")
    (store.root / "secrets.yaml").write_text("wifi_password: never-return-me\n")
    (store.root / ".hidden.yaml").write_text(SOURCE)
    (store.root / "nested").mkdir()
    (store.root / "nested/other.yaml").write_text(SOURCE)
    names = []
    offset = 0
    while offset is not None:
        result = store.list_devices(limit=1, offset=offset)
        assert result["total_files"] == 5
        assert "never-return-me" not in json.dumps(result)
        names.extend(row["filename"] for row in result["items"] + result["skipped"])
        offset = result["next_offset"]
    assert names == [
        "esphome-50.yaml",
        "esphome-67.yml",
        "package.yaml",
        "secrets.yaml",
        "solder-table.yaml",
    ]
    skipped = store.list_devices()["skipped"]
    assert "non-device" in skipped[0]["error"]["message"]
    assert skipped[1]["error"]["code"] == "ACCESS_DENIED"
    assert store.list_devices(offset=50)["next_offset"] is None


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (1, -1)])
def test_invalid_pagination_is_structured(store, limit, offset):
    result = store.list_devices(limit, offset)
    assert result["error"]["code"] == "CONFIG_INVALID"


def test_root_from_environment_and_missing_mount(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ESPHOME_CONFIG_DIR", "")
    assert ESPHome(Settings.from_env()).root == tmp_path / "esphome"
    result = ESPHome(Settings.from_env()).list_devices()
    assert result["error"]["code"] == "BUILDER_UNAVAILABLE"
    assert "not mean devices are offline" in result["error"]["message"]
    monkeypatch.setenv("ESPHOME_CONFIG_DIR", str(tmp_path))
    assert ESPHome(Settings.from_env()).list_devices()["success"]


def test_symlinks_hardlinks_and_special_files_are_not_read(store, tmp_path):
    outside = tmp_path / "private.yaml"
    outside.write_text(SOURCE + "# secret-marker\n")
    (store.root / "escape.yaml").symlink_to(outside)
    os.link(outside, store.root / "hardlink.yaml")
    os.mkfifo(store.root / "pipe.yaml")
    (store.root / "directory.yaml").mkdir()
    result = store.list_devices()
    assert [r["device"] for r in result["items"]] == ["solder-table.yaml"]
    assert len(result["skipped"]) == 4
    assert "secret-marker" not in json.dumps(result)
    link = tmp_path / "linked-root"
    link.symlink_to(store.root)
    result = ESPHome(Settings(esphome_config_dir=str(link))).list_devices()
    assert result["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize(
    "source",
    [
        "esphome: [password: TOP-SECRET\n",
        "esphome: {}\nesphome: {}\n",
        "esphome: &cycle {other: *cycle}\n",
        "esphome:\n  name: !!python/object/apply:os.system ['touch /tmp/unsafe']\n",
    ],
)
def test_untrusted_yaml_never_echoes_parser_context_or_executes(store, source):
    (store.root / "bad.yaml").write_text(source)
    result = store.list_devices()
    assert "TOP-SECRET" not in json.dumps(result)
    assert "touch /tmp/unsafe" not in json.dumps(result)
    if "!!python" not in source:
        assert result["skipped"][0]["error"]["code"] == "CONFIG_INVALID"
    else:
        assert result["items"][0]["name"] is None


def test_size_complexity_encoding_limits_and_private_metadata(store):
    (store.root / "big.yaml").write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
    (store.root / "deep.yaml").write_text("esphome: " + "[" * 50 + "]" * 50)
    (store.root / "encoding.yaml").write_bytes(b"\xff")
    (store.root / "private.yaml").write_text(
        SOURCE.replace("!secret solder_api_key", "inline-API-credential")
    )
    result = store.list_devices()
    assert len(result["skipped"]) == 3
    private = next(r for r in result["items"] if r["filename"] == "private.yaml")
    assert private["name"] is None and private["warnings"]
    assert "inline-API-credential" not in json.dumps(result)


def test_legacy_and_unexpanded_metadata(store):
    (store.root / "old.yaml").write_text(
        "esphome:\n  name: old\n  platform: ESP8266\n  board: esp01_1m\n"
    )
    (store.root / "included.yaml").write_text("esphome: !include private.yaml\n")
    (store.root / "substitution.yaml").write_text(
        "substitutions:\n  node_name: example\nesphome:\n  name: ${node_name}\n"
    )
    result = {r["filename"]: r for r in store.list_devices()["items"]}
    assert result["old.yaml"]["platform"] == "esp8266"
    assert result["old.yaml"]["board"] == "esp01_1m"
    assert result["included.yaml"]["name"] is None
    assert result["included.yaml"]["warnings"]
    assert result["substitution.yaml"]["name"] is None
    assert result["substitution.yaml"]["warnings"]
