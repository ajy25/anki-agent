# A native, self-contained app. Only public UI assets are bundled as data.
import os
from pathlib import Path
import platform
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
identity = os.environ.get("MACOS_CODESIGN_IDENTITY") or None
arch = os.environ.get("ANKIAGENT_ARCH") or platform.machine()
notices = [(str(root / "LICENSE"), ".")]
for package in ("flask", "groq", "openai", "python-dotenv", "bleach", "pywebview", "pyobjc-core"):
    notices.extend(copy_metadata(package))
analysis = Analysis([str(root / "desktop_entry.py")], pathex=[str(root)], binaries=[],
    datas=[(str(root / "ankiagent" / "templates"), "ankiagent/templates"),
           (str(root / "ankiagent" / "static"), "ankiagent/static"), *notices],
    hiddenimports=["ankiagent.app", "ankiagent.ingest", "ankiagent.llm", "ankiagent.search",
                   "webview.platforms.cocoa", "ankiagent.hotkeys"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pynput", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "wx", "tkinter",
              "torch", "transformers", "numpy", "pandas", "matplotlib", "scipy", "IPython", "pytest"],
    noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="Anki Agent",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, argv_emulation=False, target_arch=arch,
          codesign_identity=identity, entitlements_file=None)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="Anki Agent")
app = BUNDLE(collection, name="Anki Agent.app", icon=str(root / "packaging" / "AppIcon.icns"),
             bundle_identifier="org.ankiagent.desktop", version=(root / "VERSION").read_text().strip(),
             info_plist={"CFBundleDisplayName": "Anki Agent", "NSHighResolutionCapable": True,
                         "LSMinimumSystemVersion": "12.0", "NSPrincipalClass": "NSApplication"})
