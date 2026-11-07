"""Exact ESPHome source proposals, private backups and separately accepted writes."""

import difflib
import os
import re
import stat
import uuid

from .esphome import ESPHomeError
from .esphome_runtime import public_errors, require, sha, snapshot_digest


class ESPHomeChanges:
    def __init__(self, runtime, reviews):
        self.runtime, self.reviews = runtime, reviews
        self.settings = runtime.settings

    def proposal(self, proposal_id, kind="esphome_config"):
        try:
            proposal = self.reviews.status(proposal_id)
        except ValueError:
            raise ESPHomeError("CONFIG_CONFLICT", "Unknown ESPHome proposal.") from None
        if proposal["kind"] != kind:
            raise ESPHomeError(
                "ACCESS_DENIED", "Proposal belongs to a different operation."
            )
        return proposal

    @public_errors
    async def propose(self, device, config_yaml, expected_sha256):
        if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Supply the original SHA-256 returned by esphome_get_config.",
            )
        original, snapshot = self.runtime.snapshot(device, config_yaml)
        if original["sha256"] != expected_sha256:
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Source configuration changed; read it and propose again.",
            )
        if original["content"] == config_yaml:
            raise ESPHomeError("CONFIG_INVALID", "No configuration change proposed.")
        validation = require(
            await self.runtime.execute(original["device"], "validate", snapshot)
        )
        if validation.get("status") == "running":
            return {
                **validation,
                "proposal_created": False,
                "message": "Validation is still running; poll operation_id. No proposal or source write has been created.",
            }
        if not validation.get("success"):
            raise ESPHomeError("CONFIG_INVALID", "ESPHome validation did not succeed.")
        current, current_snapshot = self.runtime.snapshot(
            original["device"], config_yaml
        )
        if current["sha256"] != expected_sha256 or snapshot_digest(
            current_snapshot
        ) != snapshot_digest(snapshot):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Source or referenced secrets changed during validation; propose again.",
            )
        result = self.reviews.propose(
            {
                "kind": "esphome_config",
                "device": original["device"],
                "before": original["content"],
                "after": config_yaml,
                "expected_sha256": expected_sha256,
                "content_sha256": sha(config_yaml),
                "snapshot_sha256": snapshot_digest(snapshot),
                "validation": validation,
                "diff": "".join(
                    difflib.unified_diff(
                        original["content"].splitlines(True),
                        config_yaml.splitlines(True),
                        fromfile=original["filename"],
                        tofile=original["filename"],
                    )
                ),
                "warnings": [
                    "Toolchain validation does not establish safe GPIO wiring or physical device identity.",
                    "Coordinate with ESPHome Builder: external editors do not share this app's lock.",
                ],
                "impact": "Replace only the selected configuration. No compilation, OTA, device reboot, HA restart or Builder restart. Firmware behavior changes only after a separately accepted installation.",
            }
        )
        return {"success": True, **result}

    @public_errors
    async def validate_proposal(self, proposal_id):
        proposal = self.proposal(proposal_id)
        _, snapshot = self.runtime.snapshot(proposal["device"], proposal["after"])
        if snapshot_digest(snapshot) != proposal["snapshot_sha256"]:
            raise ESPHomeError(
                "CONFIG_CONFLICT", "Proposal secrets changed; create a fresh proposal."
            )
        return await self.runtime.execute(proposal["device"], "validate", snapshot)

    @public_errors
    async def status(self, proposal_id):
        try:
            proposal = self.reviews.status(proposal_id)
        except ValueError:
            raise ESPHomeError("CONFIG_CONFLICT", "Unknown ESPHome proposal.") from None
        if proposal["kind"] not in ("esphome_config", "esphome_install"):
            raise ESPHomeError(
                "ACCESS_DENIED", "Proposal belongs to another operation."
            )
        return {
            "success": True,
            "proposal_id": proposal_id,
            **proposal,
        }

    @public_errors
    async def apply(self, proposal_id, approved_digest):
        if not self.settings.esphome_config_writes_enabled:
            raise ESPHomeError(
                "ACCESS_DENIED",
                "ESPHome config writes are disabled; configure confirmation and enable ALLOW_ESPHOME_CONFIG_WRITES.",
            )

        async def perform(proposal):
            original, snapshot = self.runtime.snapshot(
                proposal["device"], proposal["after"]
            )
            if (
                original["sha256"] != proposal["expected_sha256"]
                or snapshot_digest(snapshot) != proposal["snapshot_sha256"]
            ):
                raise ESPHomeError(
                    "CONFIG_CONFLICT",
                    "Configuration or referenced secrets changed since review; propose again.",
                )
            temporary = ".mcp-esphome-" + uuid.uuid4().hex
            replaced = False
            with self.runtime.files.directory() as fd:
                try:
                    # Recheck under the same pinned directory immediately before replacement.
                    before, _, _ = self.runtime.files.read(fd, proposal["device"])
                    if sha(before.decode()) != proposal["expected_sha256"]:
                        raise ESPHomeError(
                            "CONFIG_CONFLICT",
                            "Source changed before writing; propose again.",
                        )
                    info = os.stat(proposal["device"], dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ESPHomeError(
                            "ACCESS_DENIED",
                            "Source must remain a regular non-linked file.",
                        )
                    output = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        stat.S_IMODE(info.st_mode),
                        dir_fd=fd,
                    )
                    with os.fdopen(output, "wb") as stream:
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                        if os.getuid() == 0:
                            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                        stream.write(proposal["after"].encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    current, _, _ = self.runtime.files.read(fd, proposal["device"])
                    if sha(current.decode()) != proposal["expected_sha256"]:
                        raise ESPHomeError(
                            "CONFIG_CONFLICT",
                            "Source changed while preparing atomic replacement; propose again.",
                        )
                    os.replace(
                        temporary, proposal["device"], src_dir_fd=fd, dst_dir_fd=fd
                    )
                    replaced = True
                    os.fsync(fd)
                    saved, _, _ = self.runtime.files.read(fd, proposal["device"])
                    if sha(saved.decode()) != proposal["content_sha256"]:
                        raise ESPHomeError(
                            "OUTCOME_UNKNOWN",
                            "Saved file differs; inspect current config and proposal backup before any retry.",
                        )
                except Exception:
                    if replaced:
                        raise ESPHomeError(
                            "OUTCOME_UNKNOWN",
                            "Replacement may have completed; inspect current configuration and the persistent proposal backup. Never repeat this apply.",
                        ) from None
                    raise
                finally:
                    try:
                        os.unlink(temporary, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            return {
                "success": True,
                "device": proposal["device"],
                "sha256": proposal["content_sha256"],
                "backup_reference": proposal_id,
                "backup_location": "management_changes.sqlite3 proposal before field; inspect esphome_change_status or propose a reviewed rollback",
            }

        try:
            return await self.reviews.apply(
                proposal_id,
                approved_digest,
                "esphome_config",
                perform,
                enabled=self.settings.esphome_config_writes_enabled,
            )
        except ValueError as exc:
            raise ESPHomeError("CONFIG_CONFLICT", str(exc)) from None

    @public_errors
    async def rollback(self, proposal_id):
        proposal = self.proposal(proposal_id)
        if proposal["status"] == "pending":
            raise ESPHomeError(
                "CONFIG_CONFLICT", "Select an applied or uncertain source proposal."
            )
        current = require(self.runtime.files.get_config(proposal["device"]))
        return await self.propose(
            proposal["device"], proposal["before"], current["sha256"]
        )
