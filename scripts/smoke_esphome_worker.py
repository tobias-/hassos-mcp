"""Exercise the real pinned toolchain over the authenticated worker HTTP protocol."""

import json
import subprocess
import sys
import time
import uuid

import httpx

SOURCE = """esphome:
  name: mcp-smoke-controller
esp8266:
  board: esp01_1m
logger:
api:
ota:
  - platform: esphome
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
"""


def main(image, compile_firmware=False):
    token = "worker-smoke-test-token-not-a-real-secret-1234"
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--user",
            "0:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "3g",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:mode=1777,size=256m",
            "--volume",
            "/data/worker",
            "-p",
            "127.0.0.1::6053",
            "-e",
            "ESPHOME_WORKER_TOKEN=" + token,
            image,
        ],
        text=True,
    ).strip()
    try:
        inspect = json.loads(
            subprocess.check_output(["docker", "inspect", container], text=True)
        )[0]
        port = inspect["NetworkSettings"]["Ports"]["6053/tcp"][0]["HostPort"]
        with httpx.Client(
            base_url="http://127.0.0.1:" + port, timeout=10, trust_env=False
        ) as client:
            for _ in range(60):
                try:
                    assert client.get("/jobs/" + "0" * 32).status_code == 401
                    break
                except httpx.TransportError:
                    time.sleep(0.2)
            client.headers["Authorization"] = "Bearer " + token
            job = uuid.uuid4().hex
            snapshot = {
                "source": SOURCE,
                "secrets": {
                    "wifi_ssid": "MCP-Test-Network",
                    "wifi_password": "not-a-real-wifi-password",
                },
            }
            result = client.post(
                "/jobs",
                json={
                    "operation_id": job,
                    "device": "test.yaml",
                    "operation": "validate",
                    "snapshot": snapshot,
                },
            ).json()
            deadline = time.monotonic() + 120
            while result.get("status") == "running" and time.monotonic() < deadline:
                time.sleep(0.3)
                result = client.get("/jobs/" + job).json()
            assert result.get("success"), result
            assert result["version"] == "2026.10.2", result
            assert "not-a-real-wifi-password" not in json.dumps(result)
            assert result["platform"] == "esp8266"
            # Same operation ID is idempotent, not a second toolchain invocation.
            repeated = client.post(
                "/jobs",
                json={
                    "operation_id": job,
                    "device": "test.yaml",
                    "operation": "validate",
                    "snapshot": snapshot,
                },
            ).json()
            assert repeated["status"] == "succeeded"
            probe = "import pathlib,sys\nfor name in sys.argv[1:]:\n try: pathlib.Path(name).read_bytes()\n except PermissionError: pass\n else: raise SystemExit('private data accessible to another job')\n"
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "20002:2000",
                    container,
                    "python3",
                    "-c",
                    probe,
                    f"/data/worker/{job}/secrets.yaml",
                    "/data/worker/jobs.sqlite3",
                ],
                check=True,
                capture_output=True,
            )
            logs = client.get(
                "/logs", params={"device": "test.yaml", "lines": 10}
            ).json()
            assert logs["success"] and logs["source"] == "build"
            assert "not-a-real-wifi-password" not in json.dumps(logs)
            print(
                "Pinned ESPHome validation, private snapshot, retained logs, authentication and idempotency passed"
            )
            if compile_firmware:
                job = uuid.uuid4().hex
                result = client.post(
                    "/jobs",
                    json={
                        "operation_id": job,
                        "device": "test.yaml",
                        "operation": "compile",
                        "snapshot": snapshot,
                    },
                ).json()
                deadline = time.monotonic() + 1810
                while result.get("status") == "running" and time.monotonic() < deadline:
                    time.sleep(2)
                    result = client.get("/jobs/" + job).json()
                assert result.get("success"), result
                artifact = client.get("/artifacts/" + job).json()
                assert (
                    artifact["success"]
                    and artifact["firmware_sha256"] == result["firmware_sha256"]
                )
                assert (
                    artifact["platform"] == "esp8266" and artifact["size_bytes"] > 10000
                )
                install = client.post(
                    "/jobs",
                    json={
                        "operation_id": uuid.uuid4().hex,
                        "device": "test.yaml",
                        "operation": "install",
                        "snapshot": snapshot,
                    },
                ).json()
                assert install["error"]["code"] == "ACCESS_DENIED"
                print(
                    "Real ESP8266 compilation, immutable artifact/hash lookup, and disabled OTA gate passed"
                )
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "2", container],
            check=True,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main(sys.argv[1], "--compile" in sys.argv[2:])
