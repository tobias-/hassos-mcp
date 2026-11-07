import hashlib
import json
import os
from pathlib import Path

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


def test_read_preserves_exact_bytes_comments_gpio_and_secret_references(store):
    data = SOURCE.replace("\n", "\r\n").encode()
    path = store.root / "solder-table.yaml"
    path.write_bytes(data)
    before = path.stat()
    result = store.get_config("solder-table")
    assert result["success"] is True
    assert result["device"] == "solder-table.yaml"
    assert result["content"].encode() == data
    assert "pin: GPIO5 # Relay, active low" in result["content"]
    assert "!secret solder_api_key" in result["content"]
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["size_bytes"] == len(data)
    assert result["redacted"] is result["truncated"] is False
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_mode == before.st_mode
    assert path.read_bytes() == data


def test_socket_config_public_identifiers_preserve_source_and_secret_references(store):
    source = (Path(__file__).parent / "fixtures/esphome-socket.yaml").read_bytes()
    (store.root / "socket-1.yaml").write_bytes(source)
    # Inspection must not resolve the secret references.
    os.mkfifo(store.root / "secrets.yaml")
    result = store.get_config("socket-1")
    assert result["success"], result
    assert result["content"].encode() == source
    assert result["sha256"] == hashlib.sha256(source).hexdigest()
    assert result["redacted"] is False
    record = next(
        r for r in store.list_devices()["items"] if r["device"] == "socket-1.yaml"
    )
    assert record["name"] == "socket-1" and not record["warnings"]


@pytest.mark.parametrize(
    "fragment",
    [
        "wifi:\n  ap:\n    ssid: Public AP\n    password: inline-password\n",
        "web_server:\n  auth:\n    username: esp\n    password: inline-password\n",
        'wifi:\n  ap:\n    ssid: "Bearer inline-token"\n',
        'web_server:\n  auth:\n    username: "https://user:password@example.invalid"\n',
        "wifi:\n  ap:\n    ssid: {password: inline-password}\n",
        "other:\n  wifi:\n    ap:\n      ssid: private-network\n",
        "other:\n  web_server:\n    auth:\n      username: private-user\n",
    ],
)
def test_public_identifier_exceptions_do_not_bypass_credential_checks(store, fragment):
    (store.root / "socket-1.yaml").write_text("esphome: {name: socket-1}\n" + fragment)
    result = store.get_config("socket-1")
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "content" not in result


@pytest.mark.parametrize(
    "fragment",
    [
        "wifi:\n  ap:\n    ssid: known-private-token\n",
        "web_server:\n  auth:\n    username: known-private-token\n",
    ],
)
def test_public_identifiers_still_reject_known_credentials(store, fragment):
    store.settings.ha_token = "known-private-token"
    (store.root / "socket-1.yaml").write_text("esphome: {name: socket-1}\n" + fragment)
    result = store.get_config("socket-1")
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "known-private-token" not in json.dumps(result)


def test_hash_changes_with_original_bytes_and_ambiguous_stems_fail(store):
    original = store.get_config("solder-table.yaml")["sha256"]
    (store.root / "solder-table.yaml").write_text(SOURCE + "# changed\n")
    assert store.get_config("solder-table")["sha256"] != original
    (store.root / "solder-table.yml").write_text(SOURCE)
    assert store.get_config("solder-table")["error"]["code"] == "CONFIG_CONFLICT"
    assert store.get_config("solder-table.yml")["success"]


@pytest.mark.parametrize(
    "device",
    [
        "../secrets.yaml",
        "/etc/passwd",
        "nested/device.yaml",
        ".hidden.yaml",
        "a\\b.yaml",
        "x\x00.yaml",
    ],
)
def test_get_rejects_caller_paths(store, device):
    result = store.get_config(device)
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "content" not in result


def test_get_resolves_only_enumerated_device_files(store):
    assert store.get_config("nonexistent")["error"]["code"] == "DEVICE_NOT_FOUND"
    (store.root / "package.yaml").write_text("switch: []\n")
    assert store.get_config("package.yaml")["error"]["code"] == "CONFIG_INVALID"
    (store.root / "secrets.yaml").write_text("private: secret-content\n")
    assert store.get_config("secrets.yaml")["error"]["code"] == "ACCESS_DENIED"
    (store.root / "link.yaml").symlink_to(store.root / "secrets.yaml")
    assert store.get_config("link.yaml")["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("!secret wifi_password", '"inline-password"'),
        SOURCE.replace("!secret solder_api_key", '"inline-encryption-key"'),
        SOURCE.replace("!secret wifi_ssid", '"private-network-name"'),
        SOURCE + "mqtt:\n  username: private-user\n",
        SOURCE + "# password: hidden-comment-credential\n",
        SOURCE + "http_request:\n  url: https://user:secret@example.invalid/\n",
        SOURCE + 'substitutions:\n  wifi_password: "escaped\\u002dpassword"\n',
        SOURCE + "# Bearer private-token-value\n",
    ],
)
def test_inline_credentials_are_rejected_without_source_snippets(store, source):
    (store.root / "solder-table.yaml").write_text(source)
    result = store.get_config("solder-table")
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "content" not in result
    assert "sha256" not in result
    assert "password" not in result["error"]["message"]


def test_known_runtime_secret_is_not_returned_even_in_comment(store, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "runtime-token-never-expose")
    (store.root / "solder-table.yaml").write_text(
        SOURCE + "# runtime-token-never-expose\n"
    )
    result = store.get_config("solder-table")
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "runtime-token-never-expose" not in json.dumps(result)


def test_secret_and_include_files_are_never_opened(store):
    # A FIFO would hang if the parser attempted to expand either reference.
    os.mkfifo(store.root / "secrets.yaml")
    os.mkfifo(store.root / "other.yaml")
    (store.root / "solder-table.yaml").write_text(
        SOURCE + "packages:\n  extra: !include other.yaml\n"
    )
    result = store.get_config("solder-table")
    assert result["success"] and "!include other.yaml" in result["content"]


def test_oversized_or_malformed_yaml_returns_error_not_truncated_content(store):
    path = store.root / "solder-table.yaml"
    path.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    result = store.get_config("solder-table")
    assert result["error"]["code"] == "CONFIG_INVALID"
    assert "no truncated YAML" in result["error"]["message"]
    assert "content" not in result
    path.write_text("esphome: [private-password\n")
    result = store.get_config("solder-table")
    assert result["error"]["code"] == "CONFIG_INVALID"
    assert "private-password" not in json.dumps(result)


def test_file_replacement_after_enumeration_cannot_follow_symlink(
    store, monkeypatch, tmp_path
):
    private = tmp_path / "private.yaml"
    private.write_text("esphome: {name: never-expose}\n")
    original = store.filenames

    def replace(fd):
        names = original(fd)
        path = store.root / "solder-table.yaml"
        path.unlink()
        path.symlink_to(private)
        return names

    monkeypatch.setattr(store, "filenames", replace)
    result = store.get_config("solder-table")
    assert result["error"]["code"] == "ACCESS_DENIED"
    assert "never-expose" not in json.dumps(result)
