"""Release assets must match the tested source, not just have plausible names."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("release_checks",
    Path(__file__).resolve().parents[1] / "packaging" / "release_checks.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.version = "1.3.1"
        self.label = f"Anki-Agent-{self.version}-macOS-arm64-preview"
        self.manifest_path = self.root / f"{self.label}.json"
        self.manifest = {"version": self.version, "architecture": "arm64", "notarized": False,
            "source_revision": "a" * 40,
            "smoke_test": {**dict.fromkeys(release.SMOKE_CHECKS, True), "architecture": "arm64"},
            "files": {}}
        for extension in ("dmg", "zip"):
            name = f"{self.label}.{extension}"
            payload = ("synthetic " + extension).encode()
            (self.root / name).write_bytes(payload)
            self.manifest["files"][name] = {"bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest()}
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def test_version_file_and_tag_must_be_exact(self):
        path = self.root / "VERSION"
        path.write_text("1.3.1\n")
        self.assertEqual(release.read_version(path), "1.3.1")
        release.validate_tag("v1.3.1", "1.3.1")
        for version in ("", "v1.3.1", "1.3", "01.3.1", "1.3.1; echo injected"):
            path.write_text(version)
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.read_version(path)
        for tag in ("v1.3.2", "1.3.1", "v1.3.1-rc1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate_tag(tag, "1.3.1")

    def test_complete_assets_match_source_revision(self):
        self.assertEqual(release.validate_artifacts(self.root, self.version, "a" * 40), self.manifest)
        with self.assertRaisesRegex(ValueError, "source revision"):
            release.validate_artifacts(self.root, self.version, "b" * 40)

    def test_changed_installer_fails_even_when_size_is_unchanged(self):
        path = self.root / f"{self.label}.dmg"
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
        with self.assertRaisesRegex(ValueError, "checksum"):
            release.validate_artifacts(self.root, self.version)

    def test_incomplete_smoke_checks_and_wrong_version_cannot_release(self):
        for field, value in (("version", "1.3.0"), ("architecture", "wrong"), ("notarized", "false")):
            original = self.manifest[field]
            self.manifest[field] = value
            self.save_manifest()
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.validate_artifacts(self.root, self.version)
            self.manifest[field] = original
        for field in release.SMOKE_CHECKS:
            self.manifest["smoke_test"][field] = False
            self.save_manifest()
            with self.subTest(check=field), self.assertRaisesRegex(ValueError, "smoke checks"):
                release.validate_artifacts(self.root, self.version)
            self.manifest["smoke_test"][field] = True

    def test_extra_private_files_and_manifest_paths_are_rejected(self):
        secret = self.root / ".env"
        secret.write_text("synthetic fixture")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            release.validate_artifacts(self.root, self.version)
        secret.unlink()
        self.manifest["files"]["../unexpected.zip"] = self.manifest["files"].pop(f"{self.label}.zip")
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "expected DMG"):
            release.validate_artifacts(self.root, self.version)

    def test_missing_installer_and_symlinks_are_rejected(self):
        path = self.root / f"{self.label}.zip"
        data = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError):
            release.validate_artifacts(self.root, self.version)
        external = self.root.parent / (self.root.name + "-external.zip")
        external.write_bytes(data)
        self.addCleanup(external.unlink)
        path.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "missing or has the wrong size"):
            release.validate_artifacts(self.root, self.version)


if __name__ == "__main__":
    unittest.main()
