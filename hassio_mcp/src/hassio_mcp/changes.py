import asyncio
import difflib
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from .ha import identifier
from .yaml_files import AutomationFiles, atomic_write, dump, parse_automation


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Changes:
    def __init__(self, settings, ha):
        self.settings, self.ha = settings, ha
        self.files = AutomationFiles(settings)
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = settings.data_dir / "changes.sqlite3"
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, created REAL, status TEXT, payload TEXT)"
            )
        os.chmod(self.db_path, 0o600)
        self.lock = asyncio.Lock()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def propose(
        self,
        automation_id: str,
        config_yaml: str | None,
        source_file: str | None = None,
        create: bool = False,
        delete: bool = False,
    ):
        identifier(automation_id)
        if delete and (create or config_yaml is not None):
            raise ValueError("Delete must omit config_yaml and cannot create")
        if not delete and config_yaml is None:
            raise ValueError("Create/edit require config_yaml")
        config = None if delete else parse_automation(config_yaml, automation_id)
        operation = "delete" if delete else "create" if create else "update"
        if source_file:
            if create:
                await self._ensure_id_absent(automation_id)
            before, after, old_config = self.files.prepare(
                source_file, automation_id, config, create=create, delete=delete
            )
            # Review the exact file rewrite, including any formatting changes.
            old_config, new_config = before, after
        else:
            # JSON APIs cannot preserve !secret / !include tags; use the YAML file path.
            try:
                config = json.loads(json.dumps(config))
            except (TypeError, ValueError):
                raise ValueError(
                    "Tagged YAML requires an allowlisted source_file"
                ) from None
            current = await self.ha.automation(automation_id)
            if create != (current is None):
                raise ValueError(
                    "Create/edit does not match the current automation; read it again"
                )
            if create:
                # Prevent shadowing an automation loaded from a package.
                states = await self.ha.states()
                if any(
                    str(s.get("attributes", {}).get("id")) == automation_id
                    for s in states
                    if s["entity_id"].startswith("automation.")
                ):
                    raise ValueError(
                        "This ID is already loaded from YAML; select its source_file"
                    )
            before, after = canonical(current), canonical(config)
            old_config, new_config = (
                dump(current) if current is not None else "",
                "" if delete else dump(config),
            )
        if before == after:
            raise ValueError("No change proposed")
        diff = "".join(
            difflib.unified_diff(
                old_config.splitlines(True),
                new_config.splitlines(True),
                fromfile="before",
                tofile="after",
            )
        )
        proposal_id = secrets.token_urlsafe(24)
        payload = {
            "operation": operation,
            "automation_id": automation_id,
            "source_file": source_file,
            "before": before,
            "after": after,
            "diff": diff,
        }
        payload["digest"] = digest(canonical(payload))
        with self.db() as db:
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, 'pending', ?)",
                (proposal_id, time.time(), canonical(payload)),
            )
        return {
            "operation": operation,
            "proposal_id": proposal_id,
            "digest": payload["digest"],
            "diff": diff,
            "expires_in_seconds": 1800,
            "approval_required": True,
            "instructions": "Show this diff and explain its effects, including deletion if requested. Wait for explicit user approval before calling apply_automation_change with this exact proposal ID and digest. Configuration changes can reload automations, stop runs, or activate triggers. Enable/disable/trigger/inspect/reload have separate directly authorized tools and do not authorize this configuration change.",
        }

    async def _ensure_id_absent(self, automation_id):
        if await self.ha.automation(automation_id) is not None or any(
            str(s.get("attributes", {}).get("id")) == automation_id
            for s in await self.ha.states()
            if s["entity_id"].startswith("automation.")
        ):
            raise ValueError("Automation ID already exists; choose a new ID")

    async def apply(self, proposal_id, approved_digest):
        if not self.settings.writes_enabled:
            raise ValueError(
                "Automation writes are disabled; enable ALLOW_AUTOMATION_WRITES after configuring ChatGPT to always confirm this tool"
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
                        "Proposal is missing or already consumed; inspect current state before proposing again"
                    )
                if time.time() - row[0] > 1800:
                    raise ValueError("Proposal expired; propose again")
                payload = json.loads(row[2])
                if not secrets.compare_digest(payload["digest"], approved_digest):
                    raise ValueError(
                        "Approved digest does not match the proposed change"
                    )
                db.execute(
                    "UPDATE proposals SET status='applying' WHERE id=?", (proposal_id,)
                )
            try:
                if payload["source_file"]:
                    await self._apply_file(payload)
                else:
                    current = await self.ha.automation(payload["automation_id"])
                    if canonical(current) != payload["before"]:
                        raise ValueError(
                            "Automation changed since review; propose again"
                        )
                    if current is None:
                        states = await self.ha.states()
                        if any(
                            str(s.get("attributes", {}).get("id"))
                            == payload["automation_id"]
                            for s in states
                            if s["entity_id"].startswith("automation.")
                        ):
                            raise ValueError(
                                "Automation ID now exists in YAML; propose again"
                            )
                    if payload.get("operation") == "delete":
                        await self.ha.delete_automation(payload["automation_id"])
                    else:
                        await self.ha.save_automation(
                            payload["automation_id"], json.loads(payload["after"])
                        )
                    saved = await self.ha.automation(payload["automation_id"])
                    if canonical(saved) != payload["after"]:
                        raise RuntimeError(
                            "Saved configuration differs; inspect before retrying"
                        )
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
            return {
                "status": "applied",
                "operation": payload.get("operation", "update"),
                "automation_id": payload["automation_id"],
                "backup_id": proposal_id,
                "note": "Configuration change applied. Inspect the automation state and traces to verify runtime behavior. Deletion may remove the entity; YAML deletion does not delete unrelated package content.",
            }

    async def _apply_file(self, payload):
        path = self.files.path(payload["source_file"])
        if path.read_text() != payload["before"]:
            raise ValueError("File changed since review; propose again")
        if (await self.ha.check_config()).get("result") != "valid":
            raise ValueError(
                "Existing HA configuration is invalid; fix it before editing"
            )
        if payload.get("operation") == "create":
            await self._ensure_id_absent(payload["automation_id"])
        if path.read_text() != payload["before"]:
            raise ValueError("File changed during validation; propose again")
        atomic_write(path, payload["after"])
        try:
            if (await self.ha.check_config()).get("result") != "valid":
                raise ValueError("HA rejected the edited configuration")
        except BaseException:
            if path.read_text() == payload["after"]:
                atomic_write(path, payload["before"])
            else:
                raise RuntimeError(
                    "Validation failed and file changed externally; backup retained, automatic rollback skipped"
                ) from None
            raise
        # A reload failure is ambiguous; retain the saved file and backup for inspection.
        await self.ha.reload()

    def status(self, proposal_id):
        with self.db() as db:
            row = db.execute(
                "SELECT created,status,payload FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        if not row:
            raise ValueError("Unknown proposal")
        data = json.loads(row[2])
        return {
            "status": row[1],
            "created": row[0],
            "automation_id": data["automation_id"],
            "operation": data.get("operation", "update"),
            "source_file": data["source_file"],
            "diff": data["diff"],
            "digest": data["digest"],
        }
