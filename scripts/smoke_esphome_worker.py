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


def main(image):
    token = "worker-smoke-test-token-not-a-real-secret-1234"
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
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
            logs = client.get(
                "/logs", params={"device": "test.yaml", "lines": 10}
            ).json()
            assert logs["success"] and logs["source"] == "build"
            assert "not-a-real-wifi-password" not in json.dumps(logs)
            print(
                "Pinned ESPHome validation, private snapshot, retained logs, authentication and idempotency passed"
            )
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "2", container],
            check=True,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main(sys.argv[1])
