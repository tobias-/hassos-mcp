"""Private ESPHome job service. Fixed operations only; no host mounts or Docker API."""

import hashlib
import hmac
import json
import os
import re
import selectors
import shutil
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
OPERATIONS = {
    "validate": 120,
    "status": 30,
    "logs": 30,
    "compile": 1800,
    "install": 180,
}


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


def protect_tree(path, group=10001, shared=False):
    """Reclaim completed private jobs; shared cache contains toolchains only."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Unsafe worker storage path")
    os.chown(path, 0, group)
    path.chmod(0o2770 if shared else 0o750)
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            child = Path(parent) / name
            if child.is_symlink():
                continue
            mode = child.stat().st_mode
            os.chown(child, 0, group)
            child.chmod(
                (0o2770 if shared else 0o750)
                if child.is_dir()
                else ((mode & 0o111) | (0o660 if shared else 0o640))
            )


class Jobs:
    def __init__(self, root, token, allow_install=False):
        self.root, self.token = Path(root), token
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "jobs.sqlite3"
        self.lock = threading.Lock()
        self.allow_install = allow_install
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
        if operation == "install":
            if not self.allow_install:
                return error("ACCESS_DENIED", "OTA is disabled on the worker.")
            artifact = self.artifact(payload.get("artifact_id", ""))
            if not artifact.get("success"):
                return artifact
            if (
                payload.get("firmware_sha256") != artifact["firmware_sha256"]
                or device != artifact["device"]
                or fingerprint(snapshot) != artifact["snapshot_sha256"]
            ):
                return error(
                    "CONFIG_CONFLICT",
                    "Artifact hash or source/secret snapshot no longer matches.",
                )
            identity = payload.get("expected_identity")
            if (
                not isinstance(identity, dict)
                or not all(
                    isinstance(identity.get(k), str)
                    for k in ("mac_address", "name", "model")
                )
                or not isinstance(payload.get("address"), str)
            ):
                return error(
                    "CONFIG_INVALID",
                    "Accepted physical identity and pinned address are required.",
                )
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
            if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= 1000:
                return error(
                    "ACCESS_DENIED",
                    "Worker history limit reached; archive/reset worker data locally before further jobs.",
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
                "SELECT id FROM jobs WHERE device=? AND operation IN ('validate','compile','install') ORDER BY updated DESC LIMIT 1",
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

    def artifact(self, artifact_id):
        result = self.status(artifact_id)
        if (
            result.get("operation") != "compile"
            or result.get("status") != "succeeded"
            or not result.get("artifact_id")
        ):
            return error("DEVICE_NOT_FOUND", "Completed compiled artifact not found.")
        path = self.root / artifact_id / "firmware.bin"
        try:
            if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != result["firmware_sha256"]:
                raise ValueError
        except (OSError, ValueError):
            return error(
                "CONFIG_CONFLICT", "Compiled artifact is missing or its hash changed."
            )
        keys = (
            "artifact_id",
            "firmware_sha256",
            "snapshot_sha256",
            "config_sha256",
            "device",
            "version",
            "platform",
            "board",
            "name",
            "size_bytes",
            "timestamp",
        )
        return {"success": True, **{key: result[key] for key in keys}}

    def run(self, payload):
        try:
            self._run(payload)
        except Exception:
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET status='unknown',result=?,updated=? WHERE id=?",
                    (
                        json.dumps(
                            error(
                                "OUTCOME_UNKNOWN",
                                "Worker could not finish or persist the operation; inspect before retrying.",
                            )
                        ),
                        time.time(),
                        payload["operation_id"],
                    ),
                )

    def _run(self, payload):
        job_id, operation = payload["operation_id"], payload["operation"]
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        (directory / "request.json").write_text(json.dumps(payload))
        with self.db() as db:
            uid = (
                20000
                + db.execute("SELECT rowid FROM jobs WHERE id=?", (job_id,)).fetchone()[
                    0
                ]
            )
        started, raw, truncated = time.monotonic(), bytearray(), False
        process = None
        secret_values = [*payload["snapshot"]["secrets"].values(), self.token]

        def safe_log():
            text = raw.decode("utf-8", "replace")
            if truncated:
                text = text.partition("\n")[2]
            # Never expose a secret prefix from a partially received log line.
            text = text.rsplit("\n", 1)[0] if "\n" in text else ""
            return redact(text, secret_values)[-MAX_LOG:]

        try:
            if operation == "install":
                artifact = self.artifact(payload["artifact_id"])
                if not artifact.get("success"):
                    raise ValueError("artifact changed")
                shutil.copyfile(
                    self.root / payload["artifact_id"] / "firmware.bin",
                    directory / "firmware.bin",
                )
            # Only this job is accessible to its distinct build UID. Private
            # retained jobs/controller files use a different group and owner.
            os.chown(directory, 0, 10001)
            directory.chmod(0o2750)
            os.chown(directory, uid, 10001)
            for child in directory.iterdir():
                child.chmod(0o640)
                os.chown(child, uid, 10001)
            # Do not pass Supervisor, tunnel, HA or worker credentials to the toolchain.
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(directory),
                "USER": "mcp-build",
                "LOGNAME": "mcp-build",
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
                user=uid,
                group=2000,
                extra_groups=[],
            )
            timed_out = False
            last_progress = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() - started > OPERATIONS[operation]:
                        timed_out = True
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                    if time.monotonic() - last_progress >= 1:
                        last_progress = time.monotonic()
                        with self.db() as db:
                            db.execute(
                                "UPDATE jobs SET result=?,updated=? WHERE id=?",
                                (
                                    json.dumps(
                                        {
                                            "stage": operation,
                                            "logs": safe_log(),
                                            "logs_truncated": truncated,
                                            "duration_ms": round(
                                                (last_progress - started) * 1000
                                            ),
                                        }
                                    ),
                                    time.time(),
                                    job_id,
                                ),
                            )
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
                    "OUTCOME_UNKNOWN"
                    if operation == "install"
                    else "OPERATION_TIMEOUT",
                    "Worker operation exceeded its time limit; never retry an uncertain installation.",
                )
            elif (directory / "result.json").is_file():
                result = json.loads((directory / "result.json").read_text())
            else:
                result = error(
                    "OUTCOME_UNKNOWN"
                    if operation == "install"
                    else "COMPILATION_FAILED"
                    if operation == "compile"
                    else "CONFIG_INVALID",
                    "ESPHome operation failed; inspect the bounded log.",
                )
            result["logs"] = safe_log()
            result["logs_truncated"] = truncated
            result["duration_ms"] = round((time.monotonic() - started) * 1000)
            result["timestamp"] = now()
            if operation == "compile" and result.get("success"):
                result["artifact_id"] = job_id
                result["snapshot_sha256"] = fingerprint(payload["snapshot"])
                result["config_sha256"] = hashlib.sha256(
                    payload["snapshot"]["source"].encode()
                ).hexdigest()

            # Redact runner-generated diagnostic text as well as stdout.
            def clean(value):
                if isinstance(value, dict):
                    # Computed hashes/version/IDs must remain exact even when a
                    # short secret coincidentally occurs in their public value.
                    exact = {
                        "version",
                        "firmware_sha256",
                        "snapshot_sha256",
                        "config_sha256",
                        "artifact_id",
                        "timestamp",
                    }
                    return {k: v if k in exact else clean(v) for k, v in value.items()}
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
            if process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            protect_tree(directory)
            protect_tree(self.root / "platformio", group=2000, shared=True)
            # Toolchain scratch data is disposable; immutable firmware, source
            # snapshot and the result remain. Shared PlatformIO packages persist.
            for name in (".esphome", "build"):
                path = directory / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
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
        if self.path == "/health":
            return self.respond({"success": True})
        if not self.authorized():
            return self.respond(
                error("ACCESS_DENIED", "Worker authentication required."), 401
            )
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/jobs/"):
            return self.respond(
                self.server.jobs.status(parsed.path.removeprefix("/jobs/"))
            )
        if parsed.path.startswith("/artifacts/"):
            return self.respond(
                self.server.jobs.artifact(parsed.path.removeprefix("/artifacts/"))
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
    if os.getuid() != 0:
        raise SystemExit(
            "Worker control requires CHOWN, SETUID, SETGID and KILL; build jobs run as isolated unprivileged users."
        )
    os.setgroups([])
    os.setgid(10001)
    protect_tree(root)
    root.chmod(0o711)  # Traverse only; individual jobs remain private.
    if options.exists():
        options.chmod(0o600)
    if root.parent == Path("/data") and not root.parent.stat().st_mode & 0o001:
        root.parent.chmod(root.parent.stat().st_mode | 0o001)
    cache = root / "platformio"
    cache.mkdir(exist_ok=True)
    protect_tree(cache, group=2000, shared=True)
    server = ThreadingHTTPServer(("0.0.0.0", 6053), Handler)
    allow_install = (
        config.get("allow_install", False)
        if options.exists()
        else os.environ.get("ALLOW_ESPHOME_INSTALL", "false").lower() == "true"
    )
    server.jobs = Jobs(root, token, allow_install)
    server.serve_forever()


if __name__ == "__main__":
    main()
