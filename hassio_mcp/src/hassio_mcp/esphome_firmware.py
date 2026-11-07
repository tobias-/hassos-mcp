"""Immutable firmware artifacts and single-use, exact-device OTA approval."""

import re
import uuid

from .esphome import ESPHomeError
from .esphome_runtime import public_errors, require, snapshot_digest


class ESPHomeFirmware:
    def __init__(self, runtime, changes):
        self.runtime, self.changes = runtime, changes
        self.reviews = changes.reviews
        self.settings = runtime.settings

    async def artifact(self, artifact_id):
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise ESPHomeError(
                "CONFIG_INVALID",
                "Use an artifact_id returned by a successful compilation.",
            )
        return require(await self.runtime.request("GET", "/artifacts/" + artifact_id))

    @public_errors
    async def compile(self, device, approved_config_proposal_id=None):
        current, snapshot = self.runtime.snapshot(device)
        if approved_config_proposal_id:
            proposal = self.changes.proposal(approved_config_proposal_id)
            if (
                proposal["status"] != "applied"
                or proposal["device"] != current["device"]
                or current["sha256"] != proposal["content_sha256"]
                or snapshot_digest(snapshot) != proposal["snapshot_sha256"]
            ):
                raise ESPHomeError(
                    "CONFIG_CONFLICT",
                    "Current source/secret snapshot is not the selected applied configuration proposal.",
                )
        return await self.runtime.execute(current["device"], "compile", snapshot)

    @public_errors
    async def propose_install(self, device, artifact_id, firmware_sha256, expected_mac):
        if not re.fullmatch(r"(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}", expected_mac):
            raise ESPHomeError(
                "CONFIG_INVALID",
                "Supply the intended device's explicit MAC address (colon-separated).",
            )
        current, snapshot = self.runtime.snapshot(device)
        artifact = await self.artifact(artifact_id)
        if (
            artifact["device"] != current["device"]
            or artifact["firmware_sha256"] != firmware_sha256
            or artifact["snapshot_sha256"] != snapshot_digest(snapshot)
        ):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Artifact, source or referenced secrets differ; compile the intended version first.",
            )
        evidence = require(
            await self.runtime.execute(current["device"], "status", snapshot)
        )
        if evidence.get("status") == "running":
            return {
                **evidence,
                "proposal_created": False,
                "message": "Identity preflight is still running. No OTA approval or installation was created.",
            }
        identity = evidence.get("identity", {})
        if evidence.get("availability") != "online":
            raise ESPHomeError(
                "DEVICE_OFFLINE",
                "A live native API identity check is required before proposing OTA.",
            )
        if (
            not evidence.get("authenticated")
            or identity.get("mac_address", "").lower() != expected_mac.lower()
        ):
            raise ESPHomeError(
                "ACCESS_DENIED",
                "An encrypted native API identity matching the intended MAC is required; no OTA proposal created.",
            )
        if (
            identity.get("name") != artifact["name"]
            or not artifact["board"]
            or identity.get("model") != artifact["board"]
        ):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Device name/board does not match the compiled artifact. Resolve compatibility before OTA.",
            )
        operation_id = uuid.uuid4().hex
        proposal = self.reviews.propose(
            {
                "kind": "esphome_install",
                "device": current["device"],
                "artifact_id": artifact_id,
                "firmware_sha256": firmware_sha256,
                "snapshot_sha256": artifact["snapshot_sha256"],
                "artifact": artifact,
                "expected_identity": identity,
                "address": evidence["address"],
                "operation_id": operation_id,
                "diff": f"Install firmware {firmware_sha256} on {current['device']} at {evidence['address']} (MAC {identity['mac_address']}, model {identity['model']}).",
                "impact": "One native OTA transfer of this exact artifact. Firmware replacement normally reboots the device and may change GPIO/relay behavior. No automatic retries or firmware rollback. Success confirms the transfer acknowledgement, not post-reboot behavior or wiring safety.",
            }
        )
        return {"success": True, **proposal}

    @public_errors
    async def install(
        self, device, artifact_id, firmware_sha256, proposal_id, approved_digest
    ):
        if not self.settings.esphome_install_enabled:
            raise ESPHomeError(
                "ACCESS_DENIED",
                "OTA is disabled; configure per-call confirmation, then enable ALLOW_ESPHOME_INSTALL in both MCP and worker.",
            )
        proposal = self.changes.proposal(proposal_id, "esphome_install")
        current = require(self.runtime.files.get_config(device))
        if (current["device"], artifact_id, firmware_sha256) != (
            proposal["device"],
            proposal["artifact_id"],
            proposal["firmware_sha256"],
        ):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Device/artifact/hash arguments do not match the exact installation proposal.",
            )

        async def perform(approved):
            _, snapshot = self.runtime.snapshot(approved["device"])
            artifact = await self.artifact(artifact_id)
            if (
                artifact != approved["artifact"]
                or snapshot_digest(snapshot) != approved["snapshot_sha256"]
            ):
                raise ESPHomeError(
                    "CONFIG_CONFLICT",
                    "Artifact or source/secret snapshot changed since review; no OTA dispatched.",
                )
            try:
                result = await self.runtime.execute(
                    approved["device"],
                    "install",
                    snapshot,
                    operation_id=approved["operation_id"],
                    extra={
                        "artifact_id": artifact_id,
                        "firmware_sha256": firmware_sha256,
                        "expected_identity": approved["expected_identity"],
                        "address": approved["address"],
                    },
                )
            except ESPHomeError:
                raise ESPHomeError(
                    "OUTCOME_UNKNOWN",
                    "Worker dispatch/result could not be confirmed. Approval is consumed. Poll this operation_id; never repeat installation automatically.",
                    operation_id=approved["operation_id"],
                ) from None
            return {
                **result,
                "approval_consumed": True,
                "operation_id": approved["operation_id"],
                "note": "Approval status records dispatch, not firmware success. Poll esphome_operation_status for the terminal installation outcome; never automatically retry.",
            }

        try:
            return await self.reviews.apply(
                proposal_id,
                approved_digest,
                "esphome_install",
                perform,
                enabled=self.settings.esphome_install_enabled,
            )
        except ValueError as exc:
            raise ESPHomeError("CONFIG_CONFLICT", str(exc)) from None
