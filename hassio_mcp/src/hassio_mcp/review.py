"""Persistent, single-use approval records for additional management tools."""

import asyncio
import difflib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from .changes import canonical, digest


class ReviewStore:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.data_dir / "management_changes.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, created REAL, status TEXT, payload TEXT)"
            )
        self.lock = asyncio.Lock()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def propose(self, payload):
        payload = dict(payload)
        if "diff" not in payload:

            def render(value):
                return (
                    ""
                    if value is None
                    else json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )

            payload["diff"] = "".join(
                difflib.unified_diff(
                    render(payload.get("before")).splitlines(True),
                    render(payload.get("after")).splitlines(True),
                    fromfile="before",
                    tofile="after",
                )
            )
        payload["digest"] = digest(canonical(payload))
        proposal_id = secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, 'pending', ?)",
                (proposal_id, time.time(), canonical(payload)),
            )
        return {
            "proposal_id": proposal_id,
            **payload,
            "approval_required": True,
            "expires_in_seconds": 1800,
            "instructions": "Show this exact change, impact and limitations. WAIT for explicit acceptance before the corresponding apply tool. Acceptance of other proposals or direct automation controls does not authorize this change.",
        }

    def status(self, proposal_id):
        with self.db() as db:
            row = db.execute(
                "SELECT created,status,payload FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        if not row:
            raise ValueError("Unknown management proposal")
        return {"created": row[0], "status": row[1], **json.loads(row[2])}

    async def apply(self, proposal_id, approved_digest, kind, perform, *, enabled=None):
        if not (
            self.settings.management_writes_enabled if enabled is None else enabled
        ):
            if enabled is not None:
                raise ValueError(
                    "This operation's write permission is disabled; configure client confirmation before enabling it"
                )
            raise ValueError(
                "Management writes are disabled; configure confirmation for all management apply tools, then enable ALLOW_MANAGEMENT_WRITES"
            )
        async with self.lock:
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT created,status,payload FROM proposals WHERE id=?",
                    (proposal_id,),
                ).fetchone()
                if not row or row[1] != "pending":
                    raise ValueError(
                        "Proposal is missing or consumed; inspect management_change_status"
                    )
                payload = json.loads(row[2])
                if payload["kind"] != kind or not secrets.compare_digest(
                    payload["digest"], approved_digest
                ):
                    raise ValueError("Proposal type or approved digest does not match")
                if time.time() - row[0] > 1800:
                    raise ValueError("Proposal expired; propose again")
                db.execute(
                    "UPDATE proposals SET status='applying' WHERE id=?", (proposal_id,)
                )
            try:
                result = await perform(payload)
            except BaseException:
                with self.db() as db:
                    db.execute(
                        "UPDATE proposals SET status='failed_or_uncertain' WHERE id=?",
                        (proposal_id,),
                    )
                raise
            with self.db() as db:
                db.execute(
                    "UPDATE proposals SET status='applied' WHERE id=?", (proposal_id,)
                )
            return {"status": "applied", "proposal_id": proposal_id, **result}
