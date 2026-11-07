import copy
import importlib.util
import json
from pathlib import Path

import pytest
from hassio_mcp.config import Settings
from hassio_mcp.esphome import ESPHomeError
from hassio_mcp.esphome_runtime import ESPHomeRuntime

SOURCE = "esphome:\n  name: controller\nesp8266:\n  board: esp01_1m\nwifi:\n  ssid: !secret ssid\n  password: !secret password\n"


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "esphome"
    root.mkdir()
    (root / "controller.yaml").write_text(SOURCE)
    (root / "secrets.yaml").write_text(
        "ssid: private-network\npassword: private-password\nunused: never-forward-this\n"
    )
    return ESPHomeRuntime(Settings(config_dir=tmp_path, data_dir=tmp_path / "data"))


def test_snapshot_only_forwards_referenced_secrets_without_expanding_source(runtime):
    original, snapshot = runtime.snapshot("controller")
    assert snapshot["source"] == SOURCE
    assert snapshot["secrets"] == {
        "ssid": "private-network",
        "password": "private-password",
    }
    assert "private-password" not in json.dumps(original)
    assert "never-forward-this" not in json.dumps(snapshot)


async def test_validation_and_logs_use_fixed_operations(runtime):
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, copy.deepcopy(payload)))
        return {"success": True, "status": "succeeded", "version": "2026.10.2"}

    runtime.request = request
    assert (await runtime.validate("controller"))["success"]
    assert calls[0][2]["operation"] == "validate"
    assert (runtime.files.root / "controller.yaml").read_text() == SOURCE
    assert (await runtime.status("controller"))["success"]
    assert calls[-1][2]["operation"] == "status"
    assert (await runtime.logs("controller", "build", 10))["success"]
    assert calls[-1][0:2] == ("GET", "/logs?device=controller.yaml&lines=10")
    assert (await runtime.logs("controller", "device", 501))["error"][
        "code"
    ] == "CONFIG_INVALID"


async def test_missing_runtime_and_unsafe_secrets_fail_structurally(runtime, tmp_path):
    assert (await runtime.validate("controller"))["error"][
        "code"
    ] == "BUILDER_UNAVAILABLE"
    secret = runtime.files.root / "secrets.yaml"
    secret.unlink()
    secret.symlink_to(tmp_path / "elsewhere")
    assert (await runtime.validate("controller"))["error"]["code"] == "ACCESS_DENIED"


def test_proposed_inline_credentials_are_not_sent(runtime):
    with pytest.raises(ESPHomeError, match="inline credentials"):
        runtime.snapshot(
            "controller", SOURCE.replace("!secret password", "private-password")
        )


def worker_module():
    spec = importlib.util.spec_from_file_location(
        "esphome_worker_test", Path(__file__).parents[1] / "esphome_worker/worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_redaction_and_restart_does_not_retry(tmp_path):
    worker = worker_module()
    assert "private-password" not in worker.redact(
        "hello private-password\npassword: other", ["private-password"]
    )
    jobs = worker.Jobs(tmp_path, "x" * 32)
    job = "a" * 32
    with jobs.db() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,'running','{}','{}',0)",
            (job, "test.yaml", "validate"),
        )
    restarted = worker.Jobs(tmp_path, "x" * 32)
    assert restarted.status(job)["status"] == "unknown"
    assert restarted.status(job)["error"]["code"] == "OUTCOME_UNKNOWN"


def test_worker_rejects_arbitrary_operations_and_bounded_input(tmp_path):
    worker = worker_module()
    jobs = worker.Jobs(tmp_path, "x" * 32)
    payload = {
        "operation_id": "b" * 32,
        "device": "test.yaml",
        "operation": "shell",
        "snapshot": {"source": SOURCE, "secrets": {}},
    }
    assert jobs.start(payload)["error"]["code"] == "CONFIG_INVALID"
    payload.update(operation="validate", device="../../test.yaml")
    assert jobs.start(payload)["error"]["code"] == "CONFIG_INVALID"
