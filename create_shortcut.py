"""Create a double-clickable macOS app using the current Python environment.

Run once with the Python environment used for launcher.py:
    python create_shortcut.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import platform
import plistlib
import subprocess
import struct
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
APP_NAME = "Anki Agent"
DEPENDENCIES = ("flask", "dotenv", "bleach", "webview", "groq", "objc", "Security")

# Finder does not inherit the terminal's activated environment or working
# directory. This bootstrap reads both from the app's generated configuration.
BOOTSTRAP = '''#!/usr/bin/python3
import os
from pathlib import Path
import plistlib
import subprocess
import sys

contents = Path(__file__).resolve().parents[1]
with (contents / "Info.plist").open("rb") as stream:
    config = plistlib.load(stream)
project = Path(config["AnkiAgentProjectPath"])
python = config["AnkiAgentPython"]

if "--check" in sys.argv[1:]:
    assert (project / "launcher.py").is_file(), "Project folder has moved; recreate the shortcut."
    code = "import importlib.util; assert all(importlib.util.find_spec(name) for name in %r)"
    subprocess.run([python, "-c", code % config["AnkiAgentDependencies"]], check=True)
    print("Anki Agent shortcut is ready.")
    sys.exit(0)

os.chdir(project)
log_dir = project / "logs"
log_dir.mkdir(exist_ok=True)
with (log_dir / "desktop.log").open("a") as log:
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
os.execv(python, [python, "-u", str(project / "launcher.py")])
'''


def build_app_icon(destination: Path) -> None:
    """Render the shared vector logo at every macOS icon size."""
    import AppKit

    image = AppKit.NSImage.alloc().initWithContentsOfFile_(
        str(ROOT / "ankiagent" / "static" / "icon.svg")
    )
    if image is None or not image.isValid():
        raise RuntimeError("Could not render the Anki Agent icon.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ankiagent-icon-") as temporary:
        iconset = Path(temporary) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                bitmap = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                    None, pixels, pixels, 8, 4, True, False,
                    AppKit.NSCalibratedRGBColorSpace, 0, 0,
                )
                AppKit.NSGraphicsContext.saveGraphicsState()
                try:
                    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
                    AppKit.NSGraphicsContext.setCurrentContext_(context)
                    context.setImageInterpolation_(AppKit.NSImageInterpolationHigh)
                    image.drawInRect_fromRect_operation_fraction_(
                        ((0, 0), (pixels, pixels)), AppKit.NSZeroRect,
                        AppKit.NSCompositingOperationSourceOver, 1.0,
                    )
                finally:
                    AppKit.NSGraphicsContext.restoreGraphicsState()
                suffix = "@2x" if scale == 2 else ""
                png = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
                (iconset / f"icon_{size}x{size}{suffix}.png").write_bytes(bytes(png))
        # Modern macOS accepts PNG-backed ICNS chunks directly. Writing the
        # container also works in headless builds where iconutil can fail.
        chunks = []
        for kind, filename in (
            (b"icp4", "icon_16x16.png"), (b"icp5", "icon_32x32.png"),
            (b"icp6", "icon_32x32@2x.png"), (b"ic07", "icon_128x128.png"),
            (b"ic08", "icon_256x256.png"), (b"ic09", "icon_512x512.png"),
            (b"ic10", "icon_512x512@2x.png"),
        ):
            png = (iconset / filename).read_bytes()
            chunks.append(kind + struct.pack(">I", len(png) + 8) + png)
        payload = b"".join(chunks)
        destination.write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + payload)


def create_shortcut() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("This shortcut is for macOS. Use python launcher.py on other systems.")
    missing = [name for name in DEPENDENCIES if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            "Desktop dependencies are missing from this Python environment: "
            + ", ".join(missing)
            + ". Activate your Anki Agent environment, install requirements.txt, and try again."
        )

    app = ROOT / f"{APP_NAME}.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / APP_NAME
    executable.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        # Use a new identifier so macOS does not reuse architecture metadata
        # from the original script-only shortcut.
        "CFBundleIdentifier": "org.ankiagent.desktop.native",
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "3",
        "CFBundleShortVersionString": "1.2",
        "CFBundleIconFile": "AppIcon.icns",
        # Script-only app bundles otherwise default to Rosetta on Apple
        # silicon, even when their Python interpreter is native ARM64.
        "LSArchitecturePriority": [platform.machine()],
        "LSRequiresNativeExecution": True,
        "NSHighResolutionCapable": True,
        "AnkiAgentProjectPath": str(ROOT),
        # Keep the environment's path: resolving a venv symlink would select
        # the base interpreter and lose the installed desktop dependencies.
        "AnkiAgentPython": sys.executable,
        "AnkiAgentDependencies": list(DEPENDENCIES),
    }
    build_app_icon(contents / "Resources" / "AppIcon.icns")
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(config, stream)
    executable.write_text(BOOTSTRAP, encoding="utf-8")
    executable.chmod(0o755)
    return app


if __name__ == "__main__":
    try:
        shortcut = create_shortcut()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    print(f"Created {shortcut}")
    print("Double-click it in Finder to open Anki Agent. You can also drag it to the Dock.")
