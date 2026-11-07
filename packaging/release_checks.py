"""Validate version tags and the exact installers promoted into a release."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SMOKE_CHECKS = ("ok", "setup", "native_webview_module", "headless", "indexing",
                "deck_switching", "source_read_only")


def read_version(path: Path = ROOT / "VERSION") -> str:
    version = path.read_text().strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("VERSION must contain a version such as 1.3.2.")
    return version


def validate_tag(tag: str, version: str) -> None:
    if tag != f"v{version}":
        raise ValueError(f"Release tag must be v{version}, matching VERSION.")


def validate_artifacts(directory: Path, version: str, revision: str | None = None) -> dict:
    manifests = list(directory.glob("*.json"))
    if len(manifests) != 1:
        raise ValueError("Expected exactly one installer manifest.")
    manifest = json.loads(manifests[0].read_text())
    if manifest.get("version") != version or manifest.get("architecture") != "arm64":
        raise ValueError("Installer version or architecture does not match this release.")
    if type(manifest.get("notarized")) is not bool:
        raise ValueError("Manifest must declare its notarization status.")
    if revision is not None and manifest.get("source_revision") != revision:
        raise ValueError("Installers were not built from the release's source revision.")
    smoke = manifest.get("smoke_test") or {}
    if any(smoke.get(check) is not True for check in SMOKE_CHECKS) or smoke.get("architecture") != "arm64":
        raise ValueError("All packaged smoke checks must pass on Apple silicon.")
    label = f"Anki-Agent-{version}-macOS-arm64" + ("" if manifest["notarized"] else "-preview")
    expected = {f"{label}.dmg", f"{label}.zip"}
    if manifests[0].name != f"{label}.json" or set(manifest.get("files", {})) != expected:
        raise ValueError("Manifest must list exactly the expected DMG and app ZIP.")
    if {path.name for path in directory.iterdir()} != expected | {manifests[0].name}:
        raise ValueError("Release directory contains missing or unexpected files.")
    for name in expected:
        path, info = directory / name, manifest["files"][name]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != info.get("bytes"):
            raise ValueError(f"Installer is missing or has the wrong size: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != info.get("sha256"):
            raise ValueError(f"Installer checksum does not match: {name}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    try:
        version = read_version()
        if args.tag:
            validate_tag(args.tag, version)
        if args.artifacts:
            validate_artifacts(args.artifacts, version, args.revision)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Release checks passed for {version}.")


if __name__ == "__main__":
    main()
