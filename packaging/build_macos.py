"""Build a portable .app, verify it in isolation, and create drag-to-install files."""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.3.1"


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--require-notarization", action="store_true")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Build Mac apps on a Mac of the target architecture.")
    identity = os.environ.get("MACOS_CODESIGN_IDENTITY")
    notary = os.environ.get("MACOS_NOTARY_PROFILE")
    if args.require_notarization and not (identity and notary):
        parser.error("Public release requires MACOS_CODESIGN_IDENTITY and MACOS_NOTARY_PROFILE.")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from create_shortcut import build_app_icon
    build_app_icon(ROOT / "packaging" / "AppIcon.icns")
    work_context = nullcontext(str(args.work_dir.resolve())) if args.work_dir else tempfile.TemporaryDirectory(prefix="ankiagent-build-")
    with work_context as temporary:
        work = Path(temporary)
        work.mkdir(parents=True, exist_ok=True)
        os.environ["PYINSTALLER_CONFIG_DIR"] = str(work / "pyinstaller-cache")
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--workpath", work / "work",
            "--distpath", work / "dist", ROOT / "packaging" / "AnkiAgent.spec", cwd=ROOT)
        app = work / "dist" / "Anki Agent.app"
        forbidden = {".env", "settings.json", "anki.db", "desktop.log", ".git"}
        assert not any(path.name in forbidden for path in app.rglob("*")), "Private files found in app"
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
        report = work / "smoke.json"
        environment = {name: value for name, value in os.environ.items()
                       if name not in ("DECK_PATH", "DECK_NAME", "GROQ_API_KEY", "OPENAI_API_KEY", "PYTHONPATH")}
        environment["ANKIAGENT_DATA_DIR"] = str(work / "fresh-user")
        run(app / "Contents" / "MacOS" / "Anki Agent", "--smoke-test", "--report", report,
            cwd=work, env=environment)
        assert json.loads(report.read_text())["ok"]
        if notary:
            if not identity:
                raise RuntimeError("Notarization also needs a Developer ID signing identity.")
            archive = work / "notarization.zip"
            run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive)
            run("xcrun", "notarytool", "submit", archive, "--keychain-profile", notary, "--wait")
            run("xcrun", "stapler", "staple", app)
            run("xcrun", "stapler", "validate", app)
        stage = work / "installer"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        (stage / "Start here.txt").write_text(
            "Welcome to Anki Agent\n\n"
            "1. Drag Anki Agent.app onto Applications.\n"
            "2. Open Anki Agent from Applications.\n"
            "3. In Setup, create and paste your own Groq API key, then click Test key.\n"
            "4. Choose your Anki profile and deck, then click Save & open deck.\n"
            "5. Leave the window open while your deck is prepared. The tutor opens when ready.\n\n"
            "Anki must already be installed with your cards on this Mac.\n"
            "You do not need Python, Terminal, or an .env file.\n\n"
            "Full setup guide and troubleshooting:\n"
            "https://github.com/ajy25/anki-agent/blob/main/docs/MAC_SETUP.md\n\n"
            + ("This build is signed and notarized.\n" if notary else
               "PREVIEW: This build has not been notarized by Apple.\n"
               "If macOS cannot verify the developer and you trust this download, try opening\n"
               "the installed app, then go to System Settings > Privacy & Security and\n"
               "click Open Anyway for Anki Agent. Confirm Open when asked.\n"
               "If the warning says the app is damaged or will damage your computer,\n"
               "stop and contact the maintainer. See the guide above for help.\n")
        )
        suffix = platform.machine()
        label = f"Anki-Agent-{VERSION}-macOS-{suffix}" + ("" if notary else "-preview")
        dmg, archive = output / f"{label}.dmg", output / f"{label}.zip"
        run("hdiutil", "create", "-volname", "Anki Agent", "-srcfolder", stage,
            "-format", "UDZO", "-ov", dmg)
        run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", stage / app.name, archive)
        if identity:
            run("codesign", "--force", "--sign", identity, "--timestamp", dmg)
        if notary:
            run("xcrun", "notarytool", "submit", dmg, "--keychain-profile", notary, "--wait")
            run("xcrun", "stapler", "staple", dmg)
            run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", dmg)
        manifest = {"version": VERSION, "architecture": suffix, "notarized": bool(notary),
                    "smoke_test": json.loads(report.read_text()), "files": {}}
        for path in (dmg, archive):
            manifest["files"][path.name] = {"bytes": path.stat().st_size,
                "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest()}
        (output / f"{label}.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
