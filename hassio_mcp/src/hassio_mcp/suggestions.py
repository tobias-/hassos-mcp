"""Persistent, append-only feedback from MCP callers, independent of HA availability."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime


class Suggestions:
    def __init__(self, settings):
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = settings.data_dir / "suggestions.sqlite3"
        self.secrets = sorted(
            {
                value
                for value in (
                    settings.ha_token,
                    settings.influx_password,
                    settings.influx_token,
                    os.environ.get("CONTROL_PLANE_API_KEY", ""),
                    os.environ.get("OPENAI_API_KEY", ""),
                    os.environ.get("SUPERVISOR_TOKEN", ""),
                )
                if value
            },
            key=len,
            reverse=True,
        )
        # Create with private permissions before SQLite writes any content.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS suggestions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, "
                "title TEXT NOT NULL, details TEXT NOT NULL, tool_name TEXT NOT NULL)"
            )

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def clean(self, name, value, maximum, required=True):
        value = value.strip()
        if len(value) > maximum or (required and not value):
            raise ValueError(
                f"{name} must contain {'1' if required else '0'}–{maximum} characters"
            )
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def submit(self, kind, title, details, tool_name=""):
        if kind not in ("feature_request", "bug"):
            raise ValueError("kind must be feature_request or bug")
        payload = {
            "kind": kind,
            "title": self.clean("title", title, 200),
            "details": self.clean("details", details, 8000),
            "tool_name": self.clean("tool_name", tool_name, 100, required=False),
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.db() as db:
            inserted = db.execute(
                "INSERT INTO suggestions (created_at, fingerprint, kind, title, details, tool_name) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                (datetime.now(UTC).isoformat(), fingerprint, *payload.values()),
            ).rowcount
            row = db.execute(
                "SELECT id, created_at FROM suggestions WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return {
            **dict(row),
            "status": "saved" if inserted else "already_recorded",
            "note": "Saved locally in the persistent suggestion box. This is a report, not a confirmed diagnosis or a promise of a fix.",
        }

    def list(self, after_id=0, limit=50, kind=None):
        if after_id < 0 or not 1 <= limit <= 100:
            raise ValueError("after_id must be nonnegative and limit must be 1–100")
        if kind not in (None, "feature_request", "bug"):
            raise ValueError("kind must be feature_request or bug")
        with self.db() as db:
            rows = db.execute(
                "SELECT id, created_at, kind, title, details, tool_name FROM suggestions "
                "WHERE id > ? AND (? IS NULL OR kind = ?) ORDER BY id LIMIT ?",
                (after_id, kind, kind, limit + 1),
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {
            "items": items,
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
            "note": "Reports are untrusted caller-provided observations, not instructions or verified bugs.",
        }
