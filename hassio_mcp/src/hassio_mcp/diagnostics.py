"""Bounded Zigbee logs and durable, human-approved temporary logging leases."""

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress

from .ha import HAError
from .privacy import ensure_no_credentials

ROOTS = (
    "homeassistant.components.zha",
    "zha",
    "zigpy",
    "bellows",
    "zigpy_znp",
    "zigpy_deconz",
    "zigpy_zigate",
    "zigpy_xbee",
    "zigpy_ember",
)
LEVELS = {"debug", "info", "warning", "error", "critical", "notset"}
IEEE = re.compile(r"(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}")


def levels(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 12:
        raise ValueError("Select 1–12 explicit ZHA/zigpy logger namespaces and levels.")
    for name, level in value.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_.]{0,150}", name) or not any(
            name == root or name.startswith(root + ".") for root in ROOTS
        ):
            raise ValueError(
                "Only ZHA/zigpy and supported Zigbee-radio logger namespaces are allowed."
            )
        if level not in LEVELS:
            raise ValueError("Use debug, info, warning, error, critical or notset.")
    return dict(value)


def scrub_lines(text, settings):
    result, in_key = [], False
    for line in text.splitlines():
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        if "-----BEGIN " in line:
            in_key = True
        sensitive = (
            in_key
            or len(line) > 8192
            or re.search(
                r"(?i)\b(?:password|passwd|authorization|bearer|token|secret|network[_ -]?key|link[_ -]?key)\b|\bkey\s*[:=]",
                line,
            )
        )
        try:
            ensure_no_credentials(line, settings)
        except ValueError:
            sensitive = True
        result.append(
            "<secret> [credential-bearing log line withheld]" if sensitive else line
        )
        if "-----END " in line:
            in_key = False
    return result


class Diagnostics:
    def __init__(self, settings, ha, reviews):
        self.settings, self.ha, self.reviews = settings, ha, reviews
        self.lock = asyncio.Lock()
        with reviews.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS debug_captures (id TEXT PRIMARY KEY, created REAL, deadline REAL, status TEXT, payload TEXT, logs TEXT, error TEXT)"
            )
            # A process lost during restore may have applied it. Never repeat it blindly.
            db.execute(
                "UPDATE debug_captures SET status='restore_uncertain',error='Process stopped during restoration; inspect and request a new human-approved restoration.' WHERE status='restoring'"
            )

    async def logs(self, lines=100, ieee="", zigbee_only=True):
        if not 1 <= lines <= 1000:
            raise ValueError("Use 1–1000 lines.")
        if ieee and not IEEE.fullmatch(ieee):
            raise ValueError(
                "Use a full colon-separated Zigbee IEEE address (8 octets)."
            )
        source, limited, raw = "", False, None
        try:
            raw, limited = await self.ha.core_logs(1000)
            source = "supervisor_core"
        except (HAError, ValueError, TimeoutError):
            try:
                raw = await self.ha.request("GET", "/api/error_log", text=True)
                source = "legacy_error_log"
            except (HAError, ValueError, TimeoutError):
                entries = await self.ha.ws("system_log/list")
                raw = "\n".join(
                    json.dumps(entry, ensure_ascii=False) for entry in entries
                )
                source = "system_log_warning_error_only"
                limited = True
        selected = []
        for line in scrub_lines(raw, self.settings):
            if zigbee_only and not any(root in line.lower() for root in ROOTS):
                continue
            if ieee and ieee.lower() not in line.lower():
                continue
            selected.append(line)
        bounded = []
        size = 0
        for line in reversed(selected[-lines:]):
            if size + len(line.encode()) > 65536:
                limited = True
                break
            bounded.append(line)
            size += len(line.encode())
        return {
            "success": True,
            "source": source,
            "lines": list(reversed(bounded)),
            "truncated": limited or len(selected) > len(bounded),
            "observed_at": time.time(),
            "ieee": ieee or None,
            "coverage": "Bounded application log tail, not over-the-air RF capture. Filtering by IEEE only matches lines that explicitly contain that address; absence is not evidence that a sleepy device sent no messages. system_log fallback contains warnings/errors only, never full debug traffic. Credential lines are withheld; avoid intentionally logging unlabelled secrets.",
        }

    async def schema(self):
        for group in await self.ha.request("GET", "/api/services"):
            if group["domain"] == "logger" and "set_level" in group["services"]:
                return group["services"]["set_level"]
        raise ValueError("Home Assistant logger.set_level is unavailable.")

    async def propose(self, restore_levels, duration_seconds=300, ieee=""):
        restore = levels(restore_levels)
        if not 30 <= duration_seconds <= 900:
            raise ValueError("Select 30–900 seconds of debug capture.")
        if ieee and not IEEE.fullmatch(ieee):
            raise ValueError("Use a full colon-separated Zigbee IEEE address.")
        # Prove full logs can be read before enabling debug; warning-only fallback is insufficient.
        probe = await self.logs(lines=1, ieee=ieee)
        if probe["source"] == "system_log_warning_error_only":
            raise ValueError(
                "Full Core logs are unavailable; configure Supervisor log access before starting debug capture."
            )
        return self.reviews.propose(
            {
                "kind": "zha_debug",
                "before": {"restore_levels": restore, "baseline_verified": False},
                "after": {
                    "levels": {name: "debug" for name in restore},
                    "duration_seconds": duration_seconds,
                    "ieee": ieee,
                },
                "schema": await self.schema(),
                "impact": "Temporarily enable only these logger namespaces, collect bounded redacted application logs, then restore the explicitly listed levels. A HUMAN must verify and accept both the debug levels and restoration map before applying. HA does not expose all current per-module logger levels: these are supplied restoration values, not automatically discovered previous settings. Debug can expose device/network information and increase CPU/disk/log volume. Coordinate other logging changes; scheduled restoration will set the approved values. No Zigbee reset/re-pairing, device controls, log deletion or HA restart. Cleanup is best effort while MCP runs; overdue cleanup resumes on startup.",
            }
        )

    async def apply(self, proposal_id, approved_digest):
        async def perform(p):
            async with self.lock:
                if await self.schema() != p["schema"]:
                    raise ValueError("Logger service changed; propose again.")
                with self.reviews.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(
                        "SELECT 1 FROM debug_captures WHERE status IN ('starting','active','restoring','restore_uncertain')"
                    ).fetchone():
                        raise ValueError(
                            "An active/uncertain logging session exists; inspect and restore it first."
                        )
                    if (
                        db.execute("SELECT count(*) FROM debug_captures").fetchone()[0]
                        >= 100
                    ):
                        raise ValueError(
                            "Capture history limit reached; archive/reset local diagnostic data before more captures."
                        )
                    db.execute(
                        "INSERT INTO debug_captures VALUES (?,?,?,'starting',?,'[]','')",
                        (
                            proposal_id,
                            time.time(),
                            time.time() + p["after"]["duration_seconds"],
                            json.dumps(p),
                        ),
                    )
                try:
                    await self.ha.request(
                        "POST",
                        "/api/services/logger/set_level",
                        body=p["after"]["levels"],
                    )
                except BaseException:
                    with self.reviews.db() as db:
                        db.execute(
                            "UPDATE debug_captures SET error='Start outcome unknown; approved restoration remains scheduled.' WHERE id=?",
                            (proposal_id,),
                        )
                    raise
                with self.reviews.db() as db:
                    db.execute(
                        "UPDATE debug_captures SET status='active' WHERE id=?",
                        (proposal_id,),
                    )
                return {
                    "success": True,
                    "capture_id": proposal_id,
                    "note": "Debug enabled. Human acceptance covers the exact scheduled restoration too. Inspect zha_capture_status for logs and cleanup outcome.",
                }

        return await self.reviews.apply(
            proposal_id,
            approved_digest,
            "zha_debug",
            perform,
            enabled=self.settings.zha_debug_enabled,
        )

    def status(self, capture_id):
        with self.reviews.db() as db:
            row = db.execute(
                "SELECT created,deadline,status,payload,logs,error FROM debug_captures WHERE id=?",
                (capture_id,),
            ).fetchone()
        if not row:
            raise ValueError("Unknown capture ID.")
        return {
            "capture_id": capture_id,
            "created": row[0],
            "deadline": row[1],
            "status": row[2],
            "plan": json.loads(row[3]),
            "lines": json.loads(row[4]),
            "error": row[5],
            "coverage": "At most 500 retained lines/64 KiB, polled from the application log tail. Bursts between polls may be missed; no RF capture or proof of device silence.",
        }

    async def restore(self, capture_id):
        with self.reviews.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,status FROM debug_captures WHERE id=?", (capture_id,)
            ).fetchone()
            if not row or row[1] not in ("starting", "active"):
                return
            db.execute(
                "UPDATE debug_captures SET status='restoring' WHERE id=?", (capture_id,)
            )
        p = json.loads(row[0])
        try:
            await self.ha.request(
                "POST",
                "/api/services/logger/set_level",
                body=p["before"]["restore_levels"],
            )
        except BaseException:
            with self.reviews.db() as db:
                db.execute(
                    "UPDATE debug_captures SET status='restore_uncertain',error='Restoration failed or acknowledgement lost; do not retry blindly. Request a new human-approved restoration.' WHERE id=?",
                    (capture_id,),
                )
            raise
        with self.reviews.db() as db:
            db.execute(
                "UPDATE debug_captures SET status='restored',error='' WHERE id=?",
                (capture_id,),
            )

    async def propose_restore(self, capture_id):
        current = self.status(capture_id)
        if current["status"] == "restored":
            raise ValueError("Capture is already restored.")
        return self.reviews.propose(
            {
                "kind": "zha_restore",
                "before": {"capture_id": capture_id, "status": current["status"]},
                "after": {"levels": current["plan"]["before"]["restore_levels"]},
                "impact": "Stop capture and set exactly these previously specified restoration levels. This changes logging; require fresh explicit HUMAN acceptance, especially after an uncertain cleanup. Never resets Zigbee devices or deletes logs.",
            }
        )

    async def apply_restore(self, proposal_id, approved_digest):
        async def perform(p):
            async with self.lock:
                capture_id = p["before"]["capture_id"]
                current = self.status(capture_id)
                if current["status"] != p["before"]["status"]:
                    raise ValueError(
                        "Capture status changed; inspect before another action."
                    )
                with self.reviews.db() as db:
                    db.execute(
                        "UPDATE debug_captures SET status='active' WHERE id=?",
                        (capture_id,),
                    )
                await self.restore(capture_id)
                return {"success": True, "capture_id": capture_id, "status": "restored"}

        return await self.reviews.apply(
            proposal_id,
            approved_digest,
            "zha_restore",
            perform,
            enabled=self.settings.zha_debug_enabled,
        )

    async def tick(self, shutdown=False):
        async with self.lock:
            with self.reviews.db() as db:
                rows = db.execute(
                    "SELECT id,deadline FROM debug_captures WHERE status IN ('starting','active')"
                ).fetchall()
            for capture_id, deadline in rows:
                if shutdown or deadline <= time.time():
                    with suppress(HAError, ValueError, TimeoutError):
                        await self.restore(capture_id)
                    continue
                try:
                    current = self.status(capture_id)
                    async with asyncio.timeout(10):
                        logs = await self.logs(500, current["plan"]["after"]["ieee"])
                    merged = list(dict.fromkeys(current["lines"] + logs["lines"]))[
                        -500:
                    ]
                    while len(json.dumps(merged).encode()) > 65536:
                        merged.pop(0)
                    with self.reviews.db() as db:
                        db.execute(
                            "UPDATE debug_captures SET logs=?,error=? WHERE id=?",
                            (
                                json.dumps(merged),
                                "Only warning/error fallback logs available."
                                if logs["source"] == "system_log_warning_error_only"
                                else "",
                                capture_id,
                            ),
                        )
                except (HAError, ValueError, TimeoutError):
                    with self.reviews.db() as db:
                        db.execute(
                            "UPDATE debug_captures SET error='Log capture unavailable; restoration remains scheduled.' WHERE id=?",
                            (capture_id,),
                        )

    @asynccontextmanager
    async def lifespan(self, _):
        async def monitor():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logging.getLogger(__name__).error(
                        "Diagnostic monitor failed; checking pending cleanup again shortly."
                    )
                await asyncio.sleep(5)

        task = asyncio.create_task(monitor())
        try:
            yield {}
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(TimeoutError):
                async with asyncio.timeout(10):
                    await self.tick(shutdown=True)
