import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "hassio_mcp"


def test_supervisor_build_context_and_version():
    yaml = YAML(typ="safe")
    manifest = yaml.load((APP / "config.yaml").read_text())
    project = tomllib.loads((APP / "pyproject.toml").read_text())
    assert manifest["version"] == project["project"]["version"]
    assert manifest["slug"] == APP.name
    assert manifest["options"]["allow_automation_writes"] is False
    assert "image" not in manifest  # Supervisor builds directly from this directory.
    assert "ports" not in manifest
    assert (
        yaml.load((ROOT / "repository.yaml").read_text())["url"]
        == "https://github.com/tobias-/hassos-mcp"
    )
    for path in (
        "Dockerfile",
        "pyproject.toml",
        "requirements.lock",
        "src/hassio_mcp/server.py",
    ):
        assert (APP / path).is_file()


def test_manual_package_contains_only_public_source():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/package_addon.py")],
        check=True,
        capture_output=True,
    )
    with tarfile.open(ROOT / "build/hassio-mcp-haos.tar.gz") as archive:
        names = archive.getnames()
        assert all(name.startswith("hassio_mcp/") for name in names)
        assert not any(
            Path(name).name.startswith(".env") or name.endswith((".pem", ".key"))
            for name in names
        )
        assert "hassio_mcp/config.yaml" in names
        assert "hassio_mcp/translations/en.yaml" in names
        assert "hassio_mcp/src/hassio_mcp/launcher.py" in names
        for member in archive.getmembers():
            assert member.isfile()
            relative = Path(member.name).relative_to("hassio_mcp")
            assert archive.extractfile(member).read() == (APP / relative).read_bytes()
