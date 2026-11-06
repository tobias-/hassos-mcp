"""Only explicitly allowlisted automation files are accessible."""

import io
import os
import tempfile
from pathlib import Path

from ruamel.yaml import YAML


def yaml_engine():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    return yaml


def dump(value):
    stream = io.StringIO()
    yaml_engine().dump(value, stream)
    return stream.getvalue()


def parse_automation(source: str, automation_id: str):
    if len(source.encode()) > 128 * 1024:
        raise ValueError("Automation exceeds 128 KiB")
    result = yaml_engine().load(source)
    if not isinstance(result, dict):
        raise ValueError("Provide a single automation mapping")
    if str(result.get("id", automation_id)) != automation_id:
        raise ValueError("Changing an automation's ID is not supported")
    result["id"] = automation_id
    if "use_blueprint" not in result:
        if not any(k in result for k in ("trigger", "triggers")) or not any(
            k in result for k in ("action", "actions")
        ):
            raise ValueError("Automation needs triggers and actions, or use_blueprint")
    return result


def atomic_write(path: Path, content: str):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".hassio-mcp-")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class AutomationFiles:
    def __init__(self, settings):
        self.root = settings.config_dir.resolve()
        self.allowed = settings.yaml_files

    def path(self, name):
        if (
            name not in self.allowed
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise ValueError("File is not explicitly allowlisted")
        path = self.root / name
        for part in [path, *path.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise ValueError("Symlinked automation files are not supported")
        if not path.resolve().is_relative_to(self.root) or path.suffix not in (
            ".yaml",
            ".yml",
        ):
            raise ValueError("Invalid automation file")
        if path.name in ("secrets.yaml", "secrets.yml"):
            raise ValueError("Secrets files cannot be exposed")
        return path

    def load(self, name):
        path = self.path(name)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Automation file exceeds 2 MiB")
        source = path.read_text()
        document = yaml_engine().load(source)
        items = (
            document
            if isinstance(document, list)
            else document.get("automation")
            if isinstance(document, dict)
            else None
        )
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise ValueError(
                "Supported files contain an automation list, or a package with an automation list"
            )
        return source, document, items

    def prepare(self, name, automation_id, new_config=None):
        source, document, items = self.load(name)
        matches = [
            i for i, item in enumerate(items) if str(item.get("id")) == automation_id
        ]
        if len(matches) != 1:
            raise ValueError("Expected exactly one existing automation ID in the file")
        old = dump(items[matches[0]])
        if new_config is None:
            return old
        # Update the existing mapping to retain comments on unchanged keys.
        item = items[matches[0]]
        for key in list(item):
            if key not in new_config:
                del item[key]
        item.update(new_config)
        return source, dump(document), old
