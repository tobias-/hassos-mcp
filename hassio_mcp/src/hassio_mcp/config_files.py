"""Explicitly allowlisted configuration editing, validation, and reviewed restoration."""

import difflib
import os
import re
import stat
from pathlib import Path

from .dependencies import Dependencies
from .privacy import ensure_no_credentials
from .yaml_files import atomic_write, yaml_engine

MAX_FILE = 256 * 1024


class ConfigFiles:
    def __init__(self, settings, ha, reviews):
        self.settings, self.ha, self.reviews = settings, ha, reviews
        self.root = settings.config_dir.absolute()
        self.dependencies = Dependencies(ha)

    def path(self, name):
        path = Path(name)
        if (
            name not in self.settings.config_files
            or path.is_absolute()
            or any(
                part in (".", "..") or part.startswith(".") or "secret" in part.lower()
                for part in name.split("/")
            )
            or path.suffix not in (".yaml", ".yml")
            or path.parts[0] == "blueprints"
        ):
            raise ValueError(
                "Configuration file is not allowlisted or is a protected path"
            )
        result = self.root / path
        for part in (result, *result.parents):
            if part.is_symlink():
                raise ValueError("Symlinked config paths are not supported")
            if part == self.root:
                break
        if not self.root.is_dir() or not result.resolve().is_relative_to(
            self.root.resolve()
        ):
            raise ValueError("Mount the connected HA configuration directory")
        return result

    def validate(self, source):
        if len(source.encode()) > MAX_FILE:
            raise ValueError("Configuration file exceeds 256 KiB")
        document = yaml_engine().load(source)
        ensure_no_credentials(document, self.settings)
        # Comments and scalar blocks can also contain known credentials.
        ensure_no_credentials(source, self.settings)
        return document

    def read(self, name):
        path = self.path(name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("Configuration must be a regular file")
                data = handle.read(MAX_FILE + 1)
        except FileNotFoundError:
            return None
        source = data.decode("utf-8")
        self.validate(source)
        return source

    async def impact(self, before, after):
        text = (before or "") + "\n" + (after or "")
        states = await self.ha.states()
        # Conservative static candidates, matched against loaded entities and config IDs.
        candidates = set(re.findall(r"\b[a-z_][a-z0-9_]*\.[a-z0-9_]+\b", text))
        ids = set(re.findall(r"(?:^|\n)\s*(?:-\s*)?id:\s*['\"]?([A-Za-z0-9_-]+)", text))
        keys = set(re.findall(r"(?:^|\n)([a-z0-9_]+):", text))
        targets = sorted(
            {
                row["entity_id"]
                for row in states
                if row["entity_id"] in candidates
                or (
                    row["entity_id"].startswith(("automation.", "scene."))
                    and str(row.get("attributes", {}).get("id")) in ids
                )
                or (
                    row["entity_id"].startswith("script.")
                    and row["entity_id"].split(".", 1)[1] in keys
                )
            }
        )
        if len(targets) > 100:
            raise ValueError(
                "More than 100 affected entities; narrow the configuration file"
            )
        return {
            "entities": targets,
            "related": [
                await self.dependencies.related("entity", target) for target in targets
            ],
            "coverage": "Conservative static references and loaded resource IDs only. Includes, templates, renamed scripts, unloaded resources and external consumers may be missed. Review the full file diff and manual impact before acceptance.",
        }

    async def propose(self, name, content, reload_domain=None):
        before = self.read(name)
        if content is not None:
            self.validate(content)
        if before == content:
            raise ValueError("No change proposed")
        if reload_domain not in (None, "automation", "script", "scene", "template"):
            raise ValueError(
                "Choose automation/script/scene/template reload, or omit it; restart requires a separate accepted service action"
            )
        diff = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                (before or "").splitlines(True),
                (content or "").splitlines(True),
                fromfile="before/" + name,
                tofile="after/" + name,
            )
        )
        return self.reviews.propose(
            {
                "kind": "config_file",
                "path": name,
                "before": before,
                "after": content,
                "diff": diff,
                "reload_domain": reload_domain,
                "impact": await self.impact(before, content),
                "validation": "Local YAML and credential checks passed. Full HA validation runs after accepted atomic replacement and before reload; rejected configuration is restored if no external edit intervened.",
                "effects": "Replace/delete this exact allowlisted file. Includes may affect other resources. Only the reviewed optional reload is performed; there is no automatic restart.",
            }
        )

    def write(self, name, source):
        path = self.path(name)
        if source is not None:
            if not path.parent.is_dir():
                raise ValueError(
                    "Create the destination directory outside this tool first"
                )
            atomic_write(path, source)
        else:
            path.unlink()
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    async def apply(self, proposal_id, approved_digest):
        async def perform(payload):
            name, before, after = payload["path"], payload["before"], payload["after"]
            if (
                self.read(name) != before
                or await self.impact(before, after) != payload["impact"]
            ):
                raise ValueError(
                    "Configuration or dependencies changed since review; propose again"
                )
            # Re-check the allowlist, content and snapshot immediately before writing.
            if after is not None:
                self.validate(after)
            if self.read(name) != before:
                raise ValueError("Configuration changed during impact inspection")
            self.write(name, after)
            try:
                if (await self.ha.check_config()).get("result") != "valid":
                    raise ValueError("HA rejected the configuration")
            except BaseException:
                if self.read(name) != after:
                    raise RuntimeError(
                        "Validation failed and file changed externally; backup retained, rollback skipped"
                    ) from None
                self.write(name, before)
                raise
            if self.read(name) != after:
                raise RuntimeError(
                    "File changed after validation; inspect before reloading"
                )
            if payload["reload_domain"]:
                await self.ha.request(
                    "POST", f"/api/services/{payload['reload_domain']}/reload", body={}
                )
            return {
                "path": name,
                "validated": True,
                "reload_domain": payload["reload_domain"],
                "note": "Stored file verified and HA configuration check passed. Inspect runtime behavior; reload failure is uncertain and is never retried automatically.",
            }

        return await self.reviews.apply(
            proposal_id, approved_digest, "config_file", perform
        )

    async def rollback(self, proposal_id):
        original = self.reviews.status(proposal_id)
        if original["kind"] != "config_file" or original["status"] == "pending":
            raise ValueError("Select an applied/uncertain configuration file proposal")
        return await self.propose(
            original["path"], original["before"], original["reload_domain"]
        )
