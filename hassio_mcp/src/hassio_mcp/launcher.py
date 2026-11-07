"""Load HAOS options or Compose environment; run the tunnel as PID 1."""

import json
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from threading import Event

from .setup_ui import start_setup_server


def run_haos(missing):
    """Keep setup accessible before credentials exist; supervise the tunnel once ready."""
    server = start_setup_server()
    stop = Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    child = None
    try:
        if missing:
            print(
                "Setup needed: open Web UI for links and instructions, then save Configuration and restart. Missing: "
                + ", ".join(missing),
                flush=True,
            )
        else:
            child = subprocess.Popen(["tunnel-client", "run"], start_new_session=True)
        while not stop.wait(0.5):
            if child is not None and child.poll() is not None:
                return child.returncode or 1
        return 0
    finally:
        if child is not None and child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        server.shutdown()
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    os.umask(0o077)
    options_path = Path("/data/options.json")
    if options_path.exists():
        options = json.loads(options_path.read_text())
        mapping = {
            "ha_url": "HA_URL",
            "ha_token": "HA_TOKEN",
            "tunnel_id": "CONTROL_PLANE_TUNNEL_ID",
            "openai_api_key": "CONTROL_PLANE_API_KEY",
            "allow_automation_writes": "ALLOW_AUTOMATION_WRITES",
            "allow_blueprint_writes": "ALLOW_BLUEPRINT_WRITES",
            "allow_management_writes": "ALLOW_MANAGEMENT_WRITES",
            "influx_url": "INFLUX_URL",
            "influx_version": "INFLUX_VERSION",
            "influx_database": "INFLUX_DATABASE",
            "influx_user": "INFLUX_USER",
            "influx_password": "INFLUX_PASSWORD",
            "influx_token": "INFLUX_TOKEN",
            "influx_org": "INFLUX_ORG",
            "influx_bucket": "INFLUX_BUCKET",
            "yaml_files": "HA_YAML_FILES",
            "config_files": "HA_CONFIG_FILES",
            "esphome_config_dir": "ESPHOME_CONFIG_DIR",
            "esphome_worker_url": "ESPHOME_WORKER_URL",
            "esphome_worker_token": "ESPHOME_WORKER_TOKEN",
        }
        for key, env in mapping.items():
            if key in options:
                value = options[key]
                os.environ[env] = (
                    json.dumps(value) if isinstance(value, (bool, list)) else str(value)
                )
        os.environ["DATA_DIR"] = "/data/state"
        os.environ["HA_CONFIG_DIR"] = "/homeassistant"
        if not os.environ.get("HA_TOKEN") and os.environ.get("SUPERVISOR_TOKEN"):
            os.environ["HA_TOKEN"] = os.environ["SUPERVISOR_TOKEN"]
            os.environ["HA_URL"] = "http://supervisor/core"
    if not os.environ.get("CONTROL_PLANE_API_KEY"):
        os.environ["CONTROL_PLANE_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")
    required = ("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "HA_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    # No HTTP MCP endpoint. The subprocess serves JSON-RPC on its private pipes.
    os.environ["MCP_COMMAND"] = "hassio-mcp"
    os.environ["HEALTH_LISTEN_ADDR"] = "127.0.0.1:8080"
    if options_path.exists():
        raise SystemExit(run_haos(missing))
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    os.execvp("tunnel-client", ["tunnel-client", "run"])


if __name__ == "__main__":
    main()
