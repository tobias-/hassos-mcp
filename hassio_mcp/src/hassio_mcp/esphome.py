"""Read-only ESPHome configuration discovery. No toolchain or network execution."""

import errno
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent
from ruamel.yaml.nodes import MappingNode, ScalarNode

from .privacy import SENSITIVE, ensure_no_credentials

MAX_CONFIG_BYTES = 256 * 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_YAML_EVENTS = 20000
MAX_YAML_DEPTH = 40
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.ya?ml", re.I)
PLATFORMS = ("esp8266", "esp32", "rp2040", "bk72xx", "rtl87xx", "ln882x", "host")
PRIVATE_FIELDS = re.compile(
    r"(?:^|_)(?:key|psk|ssid|bssid|username|identity|certificate|ca_cert)$", re.I
)


class ESPHomeError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def result(self):
        return {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }


def file_error(exc):
    if exc.errno in (errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR):
        return ESPHomeError(
            "ACCESS_DENIED", "ESPHome path is inaccessible or contains a symlink."
        )
    if exc.errno == errno.ENOENT:
        return ESPHomeError(
            "CONFIG_CONFLICT",
            "Configuration changed during inspection; list again.",
            True,
        )
    return ESPHomeError(
        "BUILDER_UNAVAILABLE", "Cannot read ESPHome configuration storage.", True
    )


def mapping(node):
    if not isinstance(node, MappingNode):
        return {}
    return {key.value: value for key, value in node.value}


def parse(source):
    """Bound work before composing nodes; never construct objects or expand tags."""
    yaml = YAML(typ="base", pure=True)
    depth = 0
    try:
        for count, event in enumerate(yaml.parse(source), 1):
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            if isinstance(event, AliasEvent):
                raise ESPHomeError(
                    "CONFIG_INVALID",
                    "YAML aliases cannot be safely inspected; use explicit values or !secret references.",
                )
            if count > MAX_YAML_EVENTS or depth > MAX_YAML_DEPTH:
                raise ESPHomeError(
                    "CONFIG_INVALID", "YAML exceeds inspection complexity limits."
                )
        document = yaml.compose(source)
        if not isinstance(document, MappingNode):
            raise ESPHomeError(
                "CONFIG_INVALID", "Configuration must be one YAML mapping."
            )

        def check(node):
            if isinstance(node, MappingNode):
                keys = set()
                for key, value in node.value:
                    if not isinstance(key, ScalarNode) or key.value in keys:
                        raise ESPHomeError(
                            "CONFIG_INVALID", "YAML has duplicate or complex keys."
                        )
                    keys.add(key.value)
                    check(value)
            elif not isinstance(node, ScalarNode):
                for child in node.value:
                    check(child)

        check(document)
        return document
    except ESPHomeError:
        raise
    except Exception:
        # Parser diagnostics can include the source line, including credentials.
        raise ESPHomeError(
            "CONFIG_INVALID", "Cannot parse configuration YAML; inspect it locally."
        ) from None


def check_credentials(document, source, settings):
    """Reject rather than return a misleading, redacted replacement document."""

    def check(node):
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if (
                    (SENSITIVE.search(key.value) or PRIVATE_FIELDS.search(key.value))
                    and value.tag != "!secret"
                    and not (
                        isinstance(value, ScalarNode)
                        and (value.value == "" or value.tag == "tag:yaml.org,2002:null")
                    )
                ):
                    raise ValueError("inline credentials")
                check(value)
        elif not isinstance(node, ScalarNode):
            for child in node.value:
                check(child)
        elif node.tag != "!secret":
            ensure_no_credentials(node.value, settings)

    try:
        check(document)
        ensure_no_credentials(source, settings)
        if re.search(
            r"(?im)^\s*#.*\b(?:password|passwd|token|secret|api[_ -]?key|psk)\s*[:=]\s*\S",
            source,
        ):
            raise ValueError("credential comment")
    except ValueError:
        raise ESPHomeError(
            "ACCESS_DENIED",
            "Configuration contains inline credentials or sensitive fields. Move them to secrets.yaml and use !secret references before reading through MCP. No content is returned.",
        ) from None


class ESPHome:
    def __init__(self, settings):
        self.settings = settings
        self.root = (
            Path(settings.esphome_config_dir)
            if settings.esphome_config_dir
            else settings.config_dir / "esphome"
        )

    @contextmanager
    def directory(self):
        # Pin every path component without following symlinks, then use relative
        # descriptor operations so replacing a directory cannot escape this root.
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ESPHomeError(
                "ACCESS_DENIED",
                "ESPHome configuration directory must be an absolute path without traversal.",
            )
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = next_fd
            yield fd
        except FileNotFoundError:
            raise ESPHomeError(
                "BUILDER_UNAVAILABLE",
                "ESPHome configuration directory is not mounted. Set esphome_config_dir (ESPHOME_CONFIG_DIR in Compose) to the directory visible inside this container. Blank uses HA_CONFIG_DIR/esphome. Missing storage does not mean devices are offline.",
            ) from None
        finally:
            os.close(fd)

    def filenames(self, fd):
        names = []
        with os.scandir(fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise ESPHomeError(
                        "CONFIG_INVALID",
                        "ESPHome root exceeds 4096 entries; select a smaller configuration directory.",
                    )
                if FILENAME.fullmatch(entry.name):
                    names.append(entry.name)
        return sorted(names)

    def read(self, fd, name):
        if "secret" in name.lower():
            raise ESPHomeError(
                "ACCESS_DENIED", "Secrets files are excluded from ESPHome inspection."
            )
        handle = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(handle, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ESPHomeError(
                    "ACCESS_DENIED",
                    "Only regular, non-linked configuration files can be read.",
                )
            data = stream.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_CONFIG_BYTES:
            raise ESPHomeError(
                "CONFIG_INVALID",
                "Configuration exceeds 256 KiB; no truncated YAML is returned.",
            )
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ESPHomeError(
                "CONFIG_CONFLICT",
                "Configuration changed while being read; retry inspection.",
                True,
            )
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ESPHomeError(
                "CONFIG_INVALID", "Configuration is not UTF-8 YAML."
            ) from None
        return data, source, datetime.fromtimestamp(after.st_mtime, UTC).isoformat()

    def record(self, name, source, modified):
        document = parse(source)
        top = mapping(document)
        if "esphome" not in top:
            raise ESPHomeError(
                "CONFIG_INVALID",
                "No top-level esphome definition; package-only or non-device YAML is not classified as a device.",
            )
        config = mapping(top["esphome"])
        warnings = []
        if not isinstance(top["esphome"], MappingNode):
            warnings.append(
                "The esphome definition is not a literal mapping; device metadata is unknown."
            )
        try:
            check_credentials(document, source, self.settings)
            private = False
        except ESPHomeError:
            private = True
            warnings.append(
                "Inline credentials detected; metadata omitted and config reading blocked. Use !secret references."
            )

        def value(node):
            if private or node is None:
                return None
            if (
                not isinstance(node, ScalarNode)
                or node.tag != "tag:yaml.org,2002:str"
                or len(node.value) > 200
                or "$" in node.value
            ):
                warnings.append(
                    "Metadata requires expansion or is not a short literal string; reported as unknown."
                )
                return None
            return node.value

        platform = next((p for p in PLATFORMS if p in top), None)
        board = mapping(top[platform]).get("board") if platform else config.get("board")
        if not platform:
            legacy = value(config.get("platform"))
            platform = (
                legacy.lower() if legacy and legacy.lower() in PLATFORMS else None
            )
        if "packages" in top or "substitutions" in top:
            warnings.append(
                "Packages, includes, and substitutions are not expanded; metadata may be incomplete."
            )
        return {
            "device": name,
            "filename": name,
            "name": value(config.get("name")),
            "friendly_name": value(config.get("friendly_name")),
            "platform": platform,
            "board": value(board),
            "config_present": True,
            "last_modified": modified,
            "hostname": None,
            "address": None,
            "status": "unknown",
            "home_assistant_registered": None,
            "warnings": sorted(set(warnings)),
        }

    def list_devices(self, limit=50, offset=0):
        try:
            if not 1 <= limit <= 100 or offset < 0:
                raise ESPHomeError(
                    "CONFIG_INVALID", "Use limit 1–100 and a nonnegative offset."
                )
            with self.directory() as fd:
                names = self.filenames(fd)
                items, skipped = [], []
                for name in names[offset : offset + limit]:
                    try:
                        _, source, modified = self.read(fd, name)
                        items.append(self.record(name, source, modified))
                    except OSError as exc:
                        skipped.append({"filename": name, **file_error(exc).result()})
                    except ESPHomeError as exc:
                        skipped.append({"filename": name, **exc.result()})
            end = offset + limit
            return {
                "success": True,
                "items": items,
                "skipped": skipped,
                "total_files": len(names),
                "next_offset": end if end < len(names) else None,
                "coverage": "Immediate YAML files in the configured ESPHome root, sorted by filename. Pagination covers both devices and skipped files. No includes, secrets, or packages are expanded. File presence proves neither Home Assistant registration nor physical connectivity; no network probes are made.",
            }
        except ESPHomeError as exc:
            return exc.result()
        except OSError as exc:
            return file_error(exc).result()
