"""Private ESPHome job service. Fixed operations only; no host mounts or Docker API."""

import hashlib
import hmac
import json
import os
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 512 * 1024
MAX_LOG = 64 * 1024
ID = re.compile(r"[a-f0-9]{32}")
DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.ya?ml", re.I)
OPERATIONS = {"validate": 120, "status": 30, "logs": 30}


def now():
    return datetime.now(UTC).isoformat()


def error(code, message, retryable=False):
    return {
        "success": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def fingerprint(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def redact(text, values):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    for value in sorted(set(str(v) for v in values if str(v)), key=len, reverse=True):
        for variant in (value, json.dumps(value)[1:-1]):
            text = (
                text.replace(variant, "[REDACTED]")
                if len(variant) >= 4
                else re.sub(
                    r"(?<!\w)" + re.escape(variant) + r"(?!\w)", "[REDACTED]", text
                )
            )
    text = re.sub(
        r"(?im)^.*(?:password|passwd|ssid|api.?key|token|encryption.?key|authorization|Bearer\s).*?$",
        "[sensitive log line withheld]",
        text,
    )
    text = re.sub(r"(://)[^/\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text


class Jobs:
    def __init__(self, root, token):
        self.root, self.token = Path(root), token
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "jobs.sqlite3"
        self.lock = threading.Lock()
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, device TEXT, operation TEXT, status TEXT, payload TEXT, result TEXT, updated REAL)"
            )
            db.execute(
                "UPDATE jobs SET status='unknown', result=? WHERE status='running'",
                (
                    json.dumps(
                        error(
                            "OUTCOME_UNKNOWN",
                            "Worker restarted during operation; inspect before retrying.",
                        )
                    ),
                ),
            )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, payload):
        operation = payload.get("operation")
        job_id, device = payload.get("operation_id", ""), payload.get("device", "")
        if (
            operation not in OPERATIONS
            or not ID.fullmatch(job_id)
            or not DEVICE.fullmatch(device)
        ):
            return error(
                "CONFIG_INVALID", "Invalid fixed worker operation or identifier."
            )
        snapshot = payload.get("snapshot")
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"source", "secrets"}
            or not isinstance(snapshot["source"], str)
            or not isinstance(snapshot["secrets"], dict)
        ):
            return error(
                "CONFIG_INVALID", "A bounded configuration snapshot is required."
            )
        if len(snapshot["source"].encode()) > 256 * 1024 or any(
            not isinstance(k, str) or not isinstance(v, (str, int, float, bool))
            for k, v in snapshot["secrets"].items()
        ):
            return error("CONFIG_INVALID", "Invalid snapshot size or secrets mapping.")
        if (
            not isinstance(payload.get("lines", 100), int)
            or not 1 <= payload.get("lines", 100) <= 500
        ):
            return error("CONFIG_INVALID", "Use 1–500 log lines.")
        encoded = json.dumps(payload, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT payload FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if previous:
                if previous[0] != encoded:
                    return error(
                        "CONFIG_CONFLICT",
                        "Operation ID already belongs to different arguments.",
                    )
                return self.status(job_id)
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                return error(
                    "CONFIG_CONFLICT",
                    "Worker is busy; only one job may run at a time.",
                    True,
                )
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,'running',?,'{}',?)",
                (job_id, device, operation, encoded, time.time()),
            )
        threading.Thread(target=self.run, args=(payload,), daemon=True).start()
        return self.status(job_id)

    def status(self, job_id):
        if not ID.fullmatch(job_id):
            return error("DEVICE_NOT_FOUND", "Unknown operation.")
        with self.db() as db:
            row = db.execute(
                "SELECT device,operation,status,result,updated FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return error("DEVICE_NOT_FOUND", "Unknown operation.")
        return {
            "operation_id": job_id,
            "device": row[0],
            "operation": row[1],
            "status": row[2],
            "updated": datetime.fromtimestamp(row[4], UTC).isoformat(),
            **json.loads(row[3]),
        }

    def logs(self, device, lines):
        if not DEVICE.fullmatch(device) or not 1 <= lines <= 500:
            return error("CONFIG_INVALID", "Invalid device or log line count.")
        with self.db() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE device=? AND operation='validate' ORDER BY updated DESC LIMIT 1",
                (device,),
            ).fetchone()
        if not row:
            return error(
                "DEVICE_NOT_FOUND", "No retained build/validation log for this device."
            )
        result = self.status(row[0])
        log = result.get("logs", "").splitlines()
        return {
            "success": True,
            "device": device,
            "source": "build",
            "operation_id": row[0],
            "timestamp": result["updated"],
            "logs": "\n".join(log[-lines:]),
            "truncated": result.get("logs_truncated", False) or len(log) > lines,
            "operation_status": result["status"],
        }

    def run(self, payload):
        job_id, operation = payload["operation_id"], payload["operation"]
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        (directory / "request.json").write_text(json.dumps(payload))
        started, raw, truncated = time.monotonic(), bytearray(), False
        process = None
        secret_values = [*payload["snapshot"]["secrets"].values(), self.token]
        try:
            # Do not pass Supervisor, tunnel, HA or worker credentials to the toolchain.
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(directory),
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "ESPHOME_NOGITIGNORE": "1",
                "PLATFORMIO_CORE_DIR": str(self.root / "platformio"),
            }
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("runner.py")),
                    operation,
                    str(directory),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            timed_out = False
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() - started > OPERATIONS[operation]:
                        timed_out = True
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            raw.extend(chunk)
                            if len(raw) > MAX_LOG:
                                truncated = True
                                del raw[:-MAX_LOG]
            process.wait(timeout=5)
            if timed_out:
                result = error(
                    "OPERATION_TIMEOUT", "Worker operation exceeded its time limit."
                )
            elif (directory / "result.json").is_file():
                result = json.loads((directory / "result.json").read_text())
            else:
                result = error(
                    "CONFIG_INVALID",
                    "ESPHome operation failed; inspect the bounded log.",
                )
            result["logs"] = redact(raw.decode("utf-8", "replace"), secret_values)[
                -MAX_LOG:
            ]
            result["logs_truncated"] = truncated
            result["duration_ms"] = round((time.monotonic() - started) * 1000)
            result["timestamp"] = now()

            # Redact runner-generated diagnostic text as well as stdout.
            def clean(value):
                if isinstance(value, dict):
                    return {k: clean(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [clean(v) for v in value]
                return redact(value, secret_values) if isinstance(value, str) else value

            result = clean(result)
        except Exception:
            result = error(
                "OUTCOME_UNKNOWN",
                "Worker failed to record a terminal result; do not blindly retry.",
            )
        finally:
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        state = (
            "succeeded"
            if result.get("success")
            else "unknown"
            if result.get("error", {}).get("code") == "OUTCOME_UNKNOWN"
            else "failed"
        )
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,updated=? WHERE id=?",
                (state, json.dumps(result), time.time(), job_id),
            )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log authorization, source, secret values or client bodies.

    def respond(self, result, status=200):
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.server.jobs.token
        )

    def do_GET(self):
        if not self.authorized():
            return self.respond(
                error("ACCESS_DENIED", "Worker authentication required."), 401
            )
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/jobs/"):
            return self.respond(
                self.server.jobs.status(parsed.path.removeprefix("/jobs/"))
            )
        if parsed.path == "/logs":
            try:
                query = parse_qs(parsed.query)
                return self.respond(
                    self.server.jobs.logs(
                        query.get("device", [""])[0],
                        int(query.get("lines", ["100"])[0]),
                    )
                )
            except ValueError:
                return self.respond(error("CONFIG_INVALID", "Invalid log arguments."))
        self.respond(error("DEVICE_NOT_FOUND", "Unknown worker endpoint."), 404)

    def do_POST(self):
        if not self.authorized():
            return self.respond(
                error("ACCESS_DENIED", "Worker authentication required."), 401
            )
        if self.path != "/jobs":
            return self.respond(
                error("DEVICE_NOT_FOUND", "Unknown worker endpoint."), 404
            )
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError
            self.respond(self.server.jobs.start(payload))
        except (ValueError, TypeError, KeyError):
            self.respond(
                error("CONFIG_INVALID", "Invalid bounded worker request."), 400
            )


def main():
    os.umask(0o077)
    options = Path("/data/options.json")
    config = json.loads(options.read_text()) if options.exists() else {}
    token = config.get("worker_token") or os.environ.get("ESPHOME_WORKER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit(
            "Configure a dedicated worker_token of at least 32 characters in both apps."
        )
    root = Path(os.environ.get("WORKER_DATA_DIR", "/data/worker"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.getuid() == 0:
        os.chown(root, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    server = ThreadingHTTPServer(("0.0.0.0", 6053), Handler)
    server.jobs = Jobs(root, token)
    server.serve_forever()


if __name__ == "__main__":
    main()
