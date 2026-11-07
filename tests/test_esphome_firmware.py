import copy
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.esphome import ESPHomeError
from hassio_mcp.esphome_changes import ESPHomeChanges
from hassio_mcp.esphome_firmware import ESPHomeFirmware
from hassio_mcp.esphome_runtime import ESPHomeRuntime, snapshot_digest
from hassio_mcp.review import ReviewStore

SOURCE = "esphome:\n  name: controller\nesp8266:\n  board: esp01_1m\napi:\nota:\n  - platform: esphome\n"
IDENTITY = {
    "name": "controller",
    "mac_address": "aa:bb:cc:dd:ee:ff",
    "model": "esp01_1m",
    "esphome_version": "2026.9.0",
}


@pytest.fixture
def firmware(tmp_path):
    root = tmp_path / "esphome"
    root.mkdir()
    (root / "controller.yaml").write_text(SOURCE)
    settings = Settings(
        config_dir=tmp_path, data_dir=tmp_path / "data", esphome_install_enabled=True
    )
    runtime = ESPHomeRuntime(settings)
    manager = ESPHomeFirmware(runtime, ESPHomeChanges(runtime, ReviewStore(settings)))
    _, snapshot = runtime.snapshot("controller")
    manager.artifact_fixture = {
        "success": True,
        "artifact_id": "a" * 32,
        "firmware_sha256": "b" * 64,
        "device": "controller.yaml",
        "snapshot_sha256": snapshot_digest(snapshot),
        "platform": "esp8266",
        "board": "esp01_1m",
        "name": "controller",
        "version": "2026.10.2",
        "timestamp": "fixed",
    }
    manager.evidence_fixture = {
        "success": True,
        "status": "succeeded",
        "availability": "online",
        "authenticated": True,
        "identity": copy.deepcopy(IDENTITY),
        "address": "192.168.1.25",
    }
    manager.calls = []

    async def request(method, path, payload=None):
        assert method == "GET" and path == "/artifacts/" + "a" * 32
        return copy.deepcopy(manager.artifact_fixture)

    async def execute(device, operation, snapshot, **kwargs):
        manager.calls.append((device, operation, kwargs))
        if operation == "status":
            return copy.deepcopy(manager.evidence_fixture)
        return {"success": True, "status": "succeeded"}

    runtime.request, runtime.execute = request, execute
    return manager


async def proposed(manager):
    result = await manager.propose_install(
        "controller", "a" * 32, "b" * 64, IDENTITY["mac_address"]
    )
    assert result.get("success"), result
    assert all(call[1] != "install" for call in manager.calls)
    return result


async def test_compilation_does_not_install_and_requires_selected_approved_version(
    firmware,
):
    assert (await firmware.compile("controller"))["success"]
    assert [call[1] for call in firmware.calls] == ["compile"]
    assert (await firmware.compile("controller", "missing"))["error"][
        "code"
    ] == "CONFIG_CONFLICT"


async def test_exact_install_approval_rechecks_and_dispatches_once(firmware):
    proposal = await proposed(firmware)
    assert proposal["expected_identity"] == IDENTITY
    assert proposal["firmware_sha256"] in proposal["diff"]
    result = await firmware.install(
        "controller", "a" * 32, "b" * 64, proposal["proposal_id"], proposal["digest"]
    )
    assert result["success"] and result["approval_consumed"]
    install = [call for call in firmware.calls if call[1] == "install"]
    assert len(install) == 1
    assert install[0][2]["operation_id"] == proposal["operation_id"]
    assert install[0][2]["extra"]["expected_identity"] == IDENTITY
    assert not (
        await firmware.install(
            "controller",
            "a" * 32,
            "b" * 64,
            proposal["proposal_id"],
            proposal["digest"],
        )
    )["success"]
    assert len([call for call in firmware.calls if call[1] == "install"]) == 1


@pytest.mark.parametrize("changed", ["mac", "model", "name", "offline", "unencrypted"])
async def test_identity_preflight_fails_closed(firmware, changed):
    evidence = firmware.evidence_fixture
    if changed in ("mac", "model", "name"):
        evidence["identity"]["mac_address" if changed == "mac" else changed] = (
            "different"
        )
    elif changed == "offline":
        evidence["availability"] = "offline"
    else:
        evidence["authenticated"] = False
    result = await firmware.propose_install(
        "controller", "a" * 32, "b" * 64, IDENTITY["mac_address"]
    )
    assert result["success"] is False
    assert all(call[1] != "install" for call in firmware.calls)


@pytest.mark.parametrize(
    "changed", ["artifact", "source", "digest", "expired", "permission", "arguments"]
)
async def test_install_rejects_changed_or_unapproved_inputs(firmware, changed):
    proposal = await proposed(firmware)
    digest, binary_hash = proposal["digest"], "b" * 64
    if changed == "artifact":
        firmware.artifact_fixture["firmware_sha256"] = "c" * 64
    elif changed == "source":
        (firmware.runtime.files.root / "controller.yaml").write_text(
            SOURCE + "# later edit\n"
        )
    elif changed == "digest":
        digest = "wrong"
    elif changed == "expired":
        with firmware.reviews.db() as db:
            db.execute("UPDATE proposals SET created=?", (time.time() - 2000,))
    elif changed == "permission":
        firmware.settings.esphome_install_enabled = False
    else:
        binary_hash = "c" * 64
    result = await firmware.install(
        "controller", "a" * 32, binary_hash, proposal["proposal_id"], digest
    )
    assert result["success"] is False
    assert all(call[1] != "install" for call in firmware.calls)


async def test_uncertain_dispatch_keeps_pollable_id_and_consumes_approval(firmware):
    proposal = await proposed(firmware)

    async def lost(*args, **kwargs):
        raise ESPHomeError("BUILDER_UNAVAILABLE", "Connection lost after request")

    firmware.runtime.execute = lost
    result = await firmware.install(
        "controller", "a" * 32, "b" * 64, proposal["proposal_id"], proposal["digest"]
    )
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert result["operation_id"] == proposal["operation_id"]
    status = await firmware.changes.status(proposal["proposal_id"])
    assert status["status"] == "failed_or_uncertain"
    assert not (
        await firmware.install(
            "controller",
            "a" * 32,
            "b" * 64,
            proposal["proposal_id"],
            proposal["digest"],
        )
    )["success"]


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        "test_worker_" + name,
        Path(__file__).parents[1] / "esphome_worker" / (name + ".py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_artifact_hash_and_single_active_job(tmp_path):
    worker = load_module("worker")
    jobs = worker.Jobs(tmp_path, "x" * 32)
    job = "a" * 32
    directory = tmp_path / job
    directory.mkdir()
    binary = directory / "firmware.bin"
    binary.write_bytes(b"compiled firmware")
    artifact = {
        "success": True,
        "artifact_id": job,
        "firmware_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "snapshot_sha256": "snapshot",
        "config_sha256": "source",
        "version": "2026.10.2",
        "platform": "esp8266",
        "board": "esp01_1m",
        "name": "controller",
        "size_bytes": binary.stat().st_size,
        "timestamp": "fixed",
    }
    with jobs.db() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,'succeeded','{}',?,0)",
            (job, "controller.yaml", "compile", json.dumps(artifact)),
        )
    assert jobs.artifact(job)["success"]
    binary.write_bytes(b"tampered")
    assert jobs.artifact(job)["error"]["code"] == "CONFIG_CONFLICT"
    with jobs.db() as db:
        db.execute("UPDATE jobs SET status='running'")
    result = jobs.start(
        {
            "operation_id": "b" * 32,
            "device": "controller.yaml",
            "operation": "compile",
            "snapshot": {"source": SOURCE, "secrets": {}},
        }
    )
    assert result["error"]["code"] == "CONFIG_CONFLICT"


@pytest.mark.parametrize("changed", [None, "mac", "hash", "transport"])
def test_runner_ota_preflight_and_one_transport_attempt(tmp_path, monkeypatch, changed):
    runner = load_module("runner")
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"approved firmware")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    request = {
        "snapshot": {"source": SOURCE, "secrets": {}},
        "expected_identity": IDENTITY,
        "address": "192.168.1.25",
        "firmware_sha256": digest,
    }
    (tmp_path / "request.json").write_text(json.dumps(request))
    config = {
        "esphome": {"name": "controller"},
        "esp8266": {"board": "esp01_1m"},
        "api": {},
        "ota": [{"platform": "esphome", "port": 8266}],
    }
    core = SimpleNamespace(
        target_platform="esp8266", name="controller", toolchain="platformio"
    )
    package = ModuleType("esphome")
    transport = SimpleNamespace(OTA_TYPE_UPDATE_APP=0)
    calls = []

    def perform(*args, **kwargs):
        calls.append((args, kwargs))
        assert args[2].read() == b"approved firmware"
        if changed == "transport":
            raise OSError("connection lost during transfer")

    transport.perform_ota = perform
    package.espota2 = transport
    monkeypatch.setitem(sys.modules, "esphome", package)
    monkeypatch.setitem(
        sys.modules, "esphome.config", SimpleNamespace(read_config=lambda _: config)
    )
    monkeypatch.setitem(
        sys.modules, "esphome.const", SimpleNamespace(__version__="test-version")
    )
    monkeypatch.setitem(
        sys.modules,
        "esphome.core",
        SimpleNamespace(CORE=core, Lambda=type("Lambda", (), {})),
    )
    monkeypatch.setitem(
        sys.modules, "esphome.log", SimpleNamespace(setup_log=lambda **_: None)
    )

    async def preflight(*args, **kwargs):
        identity = dict(IDENTITY)
        if changed == "mac":
            identity["mac_address"] = "00:00:00:00:00:00"
        return {
            "availability": "online",
            "authenticated": True,
            "identity": identity,
            "address": request["address"],
        }

    class Socket:
        def settimeout(self, _):
            pass

        def connect(self, target):
            assert target == ("192.168.1.25", 8266)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(runner, "network", preflight)
    monkeypatch.setattr(runner.socket, "socket", lambda *_: Socket())

    # asyncio creates an internal socketpair, so use a deterministic coroutine
    # runner for this isolated synchronous transport test.
    def run_coroutine(coro):
        try:
            coro.send(None)
        except StopIteration as exc:
            return exc.value
        raise AssertionError("unexpected suspension")

    monkeypatch.setattr(runner.asyncio, "run", run_coroutine)
    if changed == "hash":
        binary.write_bytes(b"different bytes")
    result = runner.run("install", tmp_path)
    if changed in ("mac", "hash"):
        assert not result["success"] and not calls
    elif changed == "transport":
        assert result["error"]["code"] == "OUTCOME_UNKNOWN" and len(calls) == 1
    else:
        assert result["success"] and len(calls) == 1


@pytest.mark.parametrize(
    "source",
    [
        SOURCE + "external_components: []\n",
        SOURCE + "packages: !include other.yaml\n",
        SOURCE + "esphome: {platformio_options: {extra_scripts: evil.py}}\n",
        SOURCE + 'sensor: [{lambda: "#include <private-file>"}]\n',
        SOURCE + 'sensor: [{lambda: !lambda "__asm__(x);"}]\n',
        SOURCE + "sensor: [{file: /data/options.json}]\n",
    ],
)
def test_runtime_rejects_host_build_hooks_and_external_inputs(source):
    with pytest.raises(ValueError):
        load_module("runner").guard(source)


def test_runtime_checks_expanded_lambdas_before_codegen():
    runner = load_module("runner")

    class Lambda:
        def __init__(self, value):
            self.value = value

    runner.guard_resolved({"on_boot": [Lambda("return 1;")]}, Lambda)
    with pytest.raises(ValueError):
        runner.guard_resolved(
            {"on_boot": [Lambda('/* prefix */ #include "/data/options.json"')]}, Lambda
        )


@pytest.mark.parametrize("availability", ["online", "timeout", "authentication"])
async def test_live_api_evidence_and_device_logs_are_bounded_and_scrubbed(
    monkeypatch, availability
):
    import asyncio

    runner = load_module("runner")
    worker = load_module("worker")
    monkeypatch.setitem(sys.modules, "worker", worker)
    monkeypatch.setitem(
        sys.modules,
        "esphome.core",
        SimpleNamespace(CORE=SimpleNamespace(address="controller.local")),
    )

    class Client:
        def __init__(self, address, port, *args, **kwargs):
            assert address == "192.168.1.25" and port == 6053
            assert kwargs["noise_psk"] == "private-api-key"

        async def connect(self, **kwargs):
            if availability == "timeout":
                raise TimeoutError()
            if availability == "authentication":
                raise ValueError("invalid key")

        async def device_info(self):
            return SimpleNamespace(**IDENTITY)

        async def disconnect(self):
            pass

        def subscribe_logs(self, callback, **kwargs):
            assert kwargs["dump_config"] is False
            callback(SimpleNamespace(message=b"secret=private-log-value"))
            return lambda: None

    async def resolve(*args, **kwargs):
        return [(2, 1, 6, "", ("192.168.1.25", 6053))]

    monkeypatch.setitem(sys.modules, "aioesphomeapi", SimpleNamespace(APIClient=Client))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    config = {"api": {"port": 6053, "encryption": {"key": "private-api-key"}}}
    result = await runner.network(config, "status", 1)
    assert (
        result["availability"]
        == {"online": "online", "timeout": "offline", "authentication": "unknown"}[
            availability
        ]
    )
    assert result["method"] == "native_api"
    if availability == "online":
        result = await runner.network(
            config, "logs", 1, secret_values=["private-log-value"]
        )
        assert "private-log-value" not in json.dumps(result)
        assert result["truncated"] is True
