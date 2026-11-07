"""Authenticated fixed-operation client for the separately isolated ESPHome worker."""

import asyncio
import hashlib
import json
import os
import re
import stat
import time
import uuid
from urllib.parse import urlencode

import httpx
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .config import validate_url
from .esphome import (
    MAX_CONFIG_BYTES,
    ESPHome,
    ESPHomeError,
    check_credentials,
    file_error,
    mapping,
    parse,
)


def sha(source):
    return hashlib.sha256(source.encode()).hexdigest()


def snapshot_digest(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require(result):
    if result.get("success") is False:
        err = result["error"]
        raise ESPHomeError(err["code"], err["message"], err.get("retryable", False))
    return result


def public_errors(function):
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except ESPHomeError as exc:
            return exc.result()
        except OSError as exc:
            return file_error(exc).result()

    return wrapped


class ESPHomeRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.files = ESPHome(settings)

    def snapshot(self, device, replacement=None):
        original = require(self.files.get_config(device))
        source = original["content"] if replacement is None else replacement
        if len(source.encode()) > MAX_CONFIG_BYTES:
            raise ESPHomeError("CONFIG_INVALID", "Configuration exceeds 256 KiB.")
        document = parse(source)
        if "esphome" not in mapping(document):
            raise ESPHomeError(
                "CONFIG_INVALID", "Expected an ESPHome device configuration."
            )
        check_credentials(document, source, self.settings)
        references = set()

        def walk(node):
            if node.tag == "!secret":
                if not isinstance(node, ScalarNode):
                    raise ESPHomeError(
                        "CONFIG_INVALID", "Secret references must be scalar names."
                    )
                references.add(node.value)
            elif isinstance(node, MappingNode):
                for _, value in node.value:
                    walk(value)
            elif isinstance(node, SequenceNode):
                for value in node.value:
                    walk(value)

        walk(document)
        secrets = {}
        if references:
            # Read only referenced values for the worker, never for MCP responses.
            with self.files.directory() as fd:
                handle = os.open(
                    "secrets.yaml",
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                with os.fdopen(handle, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ESPHomeError(
                            "ACCESS_DENIED",
                            "Secrets must be a regular non-linked file in the ESPHome root.",
                        )
                    raw = stream.read(MAX_CONFIG_BYTES + 1)
                if len(raw) > MAX_CONFIG_BYTES:
                    raise ESPHomeError(
                        "CONFIG_INVALID", "Secrets file exceeds the supported size."
                    )
                try:
                    values = mapping(parse(raw.decode("utf-8")))
                except (UnicodeError, ESPHomeError):
                    raise ESPHomeError(
                        "CONFIG_INVALID",
                        "Cannot parse ESPHome secrets locally; no values returned.",
                    ) from None
                for key in references:
                    value = values.get(key)
                    if not isinstance(value, ScalarNode) or value.tag.startswith("!"):
                        raise ESPHomeError(
                            "CONFIG_INVALID",
                            "A required ESPHome secret is missing or is not a scalar literal.",
                        )
                    secrets[key] = value.value
        return original, {"source": source, "secrets": secrets}

    async def request(self, method, path, payload=None):
        if (
            not self.settings.esphome_worker_url
            or len(self.settings.esphome_worker_token) < 32
        ):
            raise ESPHomeError(
                "BUILDER_UNAVAILABLE",
                "Configure the isolated ESPHome worker URL and matching token (at least 32 characters); the configuration mount alone cannot execute ESPHome.",
            )
        try:
            validate_url(self.settings.esphome_worker_url)
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + self.settings.esphome_worker_token
                },
            ) as client:
                async with client.stream(
                    method,
                    self.settings.esphome_worker_url.rstrip("/") + path,
                    json=payload,
                ) as response:
                    if response.status_code == 401:
                        raise ESPHomeError(
                            "ACCESS_DENIED", "ESPHome worker authentication failed."
                        )
                    if response.status_code >= 300:
                        raise ESPHomeError(
                            "BUILDER_UNAVAILABLE",
                            "ESPHome worker rejected the request.",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise ESPHomeError(
                                "BUILDER_UNAVAILABLE",
                                "ESPHome worker response exceeded its limit.",
                            )
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError("Expected structured worker response")
                    return result
        except (httpx.HTTPError, ValueError):
            raise ESPHomeError(
                "BUILDER_UNAVAILABLE",
                "ESPHome worker is unreachable or returned an invalid response.",
                True,
            ) from None

    async def execute(
        self, device, operation, snapshot, lines=100, operation_id=None, extra=None
    ):
        operation_id = operation_id or uuid.uuid4().hex
        try:
            return await self._execute(
                device, operation, snapshot, lines, operation_id, extra
            )
        except ESPHomeError as exc:
            raise ESPHomeError(
                exc.code, exc.message, exc.retryable, operation_id=operation_id
            ) from None

    async def _execute(self, device, operation, snapshot, lines, operation_id, extra):
        result = await self.request(
            "POST",
            "/jobs",
            {
                "operation_id": operation_id,
                "device": device,
                "operation": operation,
                "snapshot": snapshot,
                "lines": lines,
                **(extra or {}),
            },
        )
        deadline = time.monotonic() + 20
        while result.get("status") == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            result = await self.request("GET", "/jobs/" + operation_id)
        return result

    @public_errors
    async def operation_status(self, operation_id):
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise ESPHomeError(
                "CONFIG_INVALID", "Use an operation_id returned by an ESPHome tool."
            )
        return await self.request("GET", "/jobs/" + operation_id)

    @public_errors
    async def status(self, device):
        original, snapshot = self.snapshot(device)
        return await self.execute(original["device"], "status", snapshot)

    @public_errors
    async def logs(self, device, source="device", lines=100):
        if not 1 <= lines <= 500 or source not in ("device", "build"):
            raise ESPHomeError(
                "CONFIG_INVALID", "Choose device/build logs and 1–500 lines."
            )
        original = require(self.files.get_config(device))
        if source == "build":
            return await self.request(
                "GET",
                "/logs?" + urlencode({"device": original["device"], "lines": lines}),
            )
        _, snapshot = self.snapshot(original["device"])
        return await self.execute(original["device"], "logs", snapshot, lines)

    @public_errors
    async def validate(self, device, config_yaml=None):
        original, snapshot = self.snapshot(device, config_yaml)
        return await self.execute(original["device"], "validate", snapshot)
