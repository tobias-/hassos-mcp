"""Blueprint CRUD through HA, with persistent proposals and local YAML snapshots."""

import asyncio
import difflib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .changes import canonical, digest
from .dependencies import Dependencies
from .yaml_files import yaml_engine

DOMAINS = ("automation", "script", "template")
MAX_YAML = 128 * 1024


@dataclass(frozen=True)
class InputReference:
    name: str


def blueprint_path(domain, path):
    if domain not in DOMAINS:
        raise ValueError("Blueprint domain must be automation, script, or template")
    if (
        not isinstance(path, str)
        or len(path) > 240
        or not path.endswith(".yaml")
        or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
            or part in ("secrets.yaml", "configuration.yaml")
            for part in path.split("/")
        )
    ):
        raise ValueError("Use a relative blueprint path such as author/name.yaml")
    return path


def parse_blueprint(source, domain):
    if len(source.encode()) > MAX_YAML:
        raise ValueError("Blueprint exceeds 128 KiB")
    document = yaml_engine().load(source)
    budget = [10000]

    def normalize(value, depth=0):
        budget[0] -= 1
        if depth > 50 or budget[0] < 0:
            raise ValueError("Blueprint YAML is too deeply nested or complex")
        tag = str(getattr(value, "tag", ""))
        if tag == "!input":
            if not isinstance(getattr(value, "value", None), str):
                raise ValueError("!input must reference an input name")
            return InputReference(value.value)
        if tag not in (
            "",
            "None",
            *(
                "tag:yaml.org,2002:" + name
                for name in ("map", "seq", "str", "int", "float", "bool", "null")
            ),
        ):
            raise ValueError(
                "Only !input custom YAML tags are supported; no !include or !secret"
            )
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("Blueprint mapping keys must be strings")
            return {str(k): normalize(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v, depth + 1) for v in value]
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise ValueError("Unsupported blueprint YAML value")

    normalized = normalize(document)
    if not isinstance(normalized, dict) or not isinstance(
        normalized.get("blueprint"), dict
    ):
        raise ValueError("Provide a complete blueprint mapping with blueprint metadata")
    metadata = normalized["blueprint"]
    if (
        metadata.get("domain") != domain
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"].strip()
    ):
        raise ValueError("Blueprint needs a name and a matching domain")
    metadata.setdefault("input", {})
    if not isinstance(metadata["input"], dict):
        raise ValueError("Blueprint inputs must be a mapping")
    return normalized


def validate_consumer_inputs(source, domain, consumers):
    document = parse_blueprint(source, domain)
    definitions = {}
    for key, value in document["blueprint"]["input"].items():
        entries = (
            value["input"]
            if isinstance(value, dict) and "input" in value
            else {key: value}
        )
        if not isinstance(entries, dict):
            raise ValueError("Blueprint input sections must contain a mapping")
        for name, definition in entries.items():
            if name in definitions:
                raise ValueError("Duplicate blueprint input name")
            definitions[name] = definition
    references = set()

    def walk(value):
        if isinstance(value, InputReference):
            references.add(value.name)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    if references - definitions.keys():
        raise ValueError(
            "Undefined !input references: "
            + ", ".join(sorted(references - definitions.keys()))
        )
    for consumer in consumers:
        supplied = consumer["use_blueprint"].get("input", {})
        missing = [
            name
            for name, definition in definitions.items()
            if name not in supplied
            and not (isinstance(definition, dict) and "default" in definition)
        ]
        if missing:
            raise ValueError(
                f"Consumer {consumer['entity_id']} lacks inputs/defaults: {', '.join(sorted(missing))}. Supply defaults or update consumers separately first."
            )


class Blueprints:
    def __init__(self, settings, ha):
        self.settings, self.ha = settings, ha
        self.dependencies = Dependencies(ha)
        self.root = settings.config_dir.absolute()
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = settings.data_dir / "blueprint_changes.sqlite3"
        fd = os.open(self.db_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, created REAL, status TEXT, payload TEXT)"
            )
        self.lock = asyncio.Lock()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def file(self, domain, path):
        blueprint_path(domain, path)
        if not self.root.is_dir():
            raise ValueError(
                "Mount the connected Home Assistant configuration at HA_CONFIG_DIR to read and review blueprint YAML"
            )
        target = self.root / "blueprints" / domain / path
        for part in (target, *target.parents):
            if part.is_symlink():
                raise ValueError("Symlinked blueprint paths are not supported")
            if part == self.root:
                break
        if not target.resolve().is_relative_to(
            self.root.resolve() / "blueprints" / domain
        ):
            raise ValueError("Blueprint path escapes its domain directory")
        return target

    def read(self, domain, path):
        target = self.file(domain, path)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("Blueprint must be a regular YAML file")
                raw = handle.read(MAX_YAML + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_YAML:
            raise ValueError("Blueprint exceeds 128 KiB")
        return raw.decode("utf-8")

    async def list(self, domain):
        if domain not in DOMAINS:
            raise ValueError("Blueprint domain must be automation, script, or template")
        result = await self.ha.ws("blueprint/list", domain=domain)
        return [{"path": key, **value} for key, value in sorted(result.items())]

    async def propose(self, operation, domain, path, config_yaml=None):
        blueprint_path(domain, path)
        if operation not in ("create", "update", "delete"):
            raise ValueError("Operation must be create, update, or delete")
        before = self.read(domain, path)
        if (operation == "create") != (before is None):
            raise ValueError(
                "Create requires an absent blueprint; update/delete require an existing blueprint"
            )
        if operation == "delete":
            if config_yaml is not None:
                raise ValueError("Delete must omit config_yaml")
            after = None
        else:
            if config_yaml is None:
                raise ValueError("Create/update require the complete config_yaml")
            parse_blueprint(config_yaml, domain)
            after = config_yaml
        if before == after:
            raise ValueError("No change proposed")
        impact = await self.dependencies.blueprint_consumers(domain, path)
        if after is not None:
            validate_consumer_inputs(after, domain, impact["consumers"])
        diff = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile=f"before/blueprints/{domain}/{path}",
                tofile=f"after/blueprints/{domain}/{path}",
            )
        )
        payload = {
            "operation": operation,
            "domain": domain,
            "path": path,
            "before": before,
            "after": after,
            "diff": diff,
            "impact": impact,
        }
        payload["digest"] = digest(canonical(payload))
        proposal_id = secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, 'pending', ?)",
                (proposal_id, time.time(), canonical(payload)),
            )
        return {
            "proposal_id": proposal_id,
            "digest": payload["digest"],
            "operation": operation,
            "domain": domain,
            "path": path,
            "diff": diff,
            "impact": impact,
            "expires_in_seconds": 1800,
            "approval_required": True,
            "instructions": "Show this exact diff and operation to the user and WAIT for explicit acceptance before apply_blueprint_change. Updating can reload all consumers and activate behavior. Deleting can break references; HA may reject in-use blueprints. HA validates and may normalize YAML formatting/defaults. This proposal performs no HA write.",
        }

    async def apply(self, proposal_id, approved_digest):
        if not self.settings.blueprint_writes_enabled:
            raise ValueError(
                "Blueprint writes are disabled; configure ChatGPT to always confirm apply_blueprint_change, then enable ALLOW_BLUEPRINT_WRITES"
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
                        "Proposal is missing or already consumed; inspect blueprint_change_status"
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
                domain, path = payload["domain"], payload["path"]
                if self.read(domain, path) != payload["before"]:
                    raise ValueError("Blueprint changed since review; propose again")
                if (
                    "impact" in payload
                    and await self.dependencies.blueprint_consumers(domain, path)
                    != payload["impact"]
                ):
                    raise ValueError(
                        "Blueprint consumers or inputs changed since review; propose again"
                    )
                if payload["operation"] == "delete":
                    await self.ha.ws("blueprint/delete", domain=domain, path=path)
                    if self.read(domain, path) is not None:
                        raise RuntimeError(
                            "Blueprint still exists after delete; inspect before retrying"
                        )
                else:
                    expected = parse_blueprint(payload["after"], domain)
                    await self.ha.ws(
                        "blueprint/save",
                        domain=domain,
                        path=path,
                        yaml=payload["after"],
                        allow_override=payload["operation"] == "update",
                    )
                    saved = self.read(domain, path)
                    if saved is None or parse_blueprint(saved, domain) != expected:
                        raise RuntimeError(
                            "Saved blueprint differs from the proposal; inspect before retrying"
                        )
                    if (
                        "impact" in payload
                        and await self.dependencies.blueprint_consumers(domain, path)
                        != payload["impact"]
                    ):
                        raise RuntimeError(
                            "Blueprint consumer references changed; inspect before retrying"
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
                "operation": payload["operation"],
                "domain": domain,
                "path": path,
                "backup_id": proposal_id,
                "note": "HA accepted the change and the mounted file was verified. Inspect affected consumers and traces; an update may reload them.",
            }

    def status(self, proposal_id):
        with self.db() as db:
            row = db.execute(
                "SELECT created,status,payload FROM proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        if not row:
            raise ValueError("Unknown blueprint proposal")
        payload = json.loads(row[2])
        return {"status": row[1], "created": row[0], **payload}

    async def propose_rollback(self, proposal_id):
        original = self.status(proposal_id)
        if original["status"] == "pending":
            raise ValueError(
                "This proposal has not been applied; there is nothing to roll back"
            )
        domain, path = original["domain"], original["path"]
        current = self.read(domain, path)
        before = original["before"]
        if current == before:
            raise ValueError("Blueprint already matches the backup")
        operation = (
            "delete" if before is None else "create" if current is None else "update"
        )
        return {
            **await self.propose(operation, domain, path, before),
            "restores_proposal_id": proposal_id,
            "rollback_note": "This is a NEW proposal against current content. Show its diff and impact and obtain fresh acceptance; the old approval never authorizes restoration.",
        }
