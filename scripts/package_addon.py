"""Package the canonical HAOS app, copying only an explicit source allowlist."""

import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hassio_mcp"
OUT = ROOT / "build" / "hassio_mcp"


def source_files():
    names = (
        "pyproject.toml",
        "requirements.lock",
        ".dockerignore",
        "config.yaml",
        "Dockerfile",
        "README.md",
        "DOCS.md",
        "CHANGELOG.md",
    )
    return [SOURCE / name for name in names] + sorted((SOURCE / "src").rglob("*.py"))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = source_files()
    for source in files:
        target = OUT / source.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    with tarfile.open(ROOT / "build/hassio-mcp-haos.tar.gz", "w:gz") as archive:
        for source in files:
            archive.add(
                source,
                arcname=str(Path("hassio_mcp") / source.relative_to(SOURCE)),
                recursive=False,
            )
    print("Built build/hassio-mcp-haos.tar.gz; install hassio_mcp/ under HAOS /addons/")


if __name__ == "__main__":
    main()
