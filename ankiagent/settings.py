"""Per-user desktop preferences and a private, app-managed .env file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from dotenv import dotenv_values, set_key


def data_dir() -> Path:
    override = os.environ.get("ANKIAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Anki Agent"
    return Path.home() / ".local" / "share" / "anki-agent"


class EnvCredentials:
    """Keep credentials outside the signed bundle so updates preserve them.

    This is plaintext storage, restricted to the current user (0600). Never
    copy this file into builds or return its contents through the setup API.
    """
    def __init__(self, root: Path):
        self.path = root / ".env"

    def get(self) -> str:
        try:
            if not self.path.exists():
                return ""
            # Treat the saved key literally, without expanding environment vars.
            return dotenv_values(self.path, interpolate=False).get("GROQ_API_KEY") or ""
        except (OSError, UnicodeError):
            raise RuntimeError("Could not read the saved API key. Open Setup and save your key again.") from None

    def set(self, key: str) -> None:
        if not key or any(char.isspace() or char == "\x00" for char in key):
            raise ValueError("Enter an API key without spaces or line breaks.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="credentials-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                if self.path.exists():
                    stream.write(self.path.read_text())
            # Preserve unrelated entries/comments if the user edited this file.
            set_key(temporary, "GROQ_API_KEY", key, quote_mode="always")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except (OSError, UnicodeError):
            raise RuntimeError("Could not save the API key. Check access to Anki Agent's settings folder and try again.") from None
        finally:
            Path(temporary).unlink(missing_ok=True)


class SettingsStore:
    def __init__(self, root: Path | None = None, vault=None):
        self.root = root or data_dir()
        self.path = self.root / "settings.json"
        self.vault = vault if vault is not None else EnvCredentials(self.root)

    def get_key(self) -> str:
        saved = self.vault.get()
        # Source launches can supply an initial key via the environment. Once
        # Setup has been saved, a missing file must not revive a stale env key.
        return saved or (os.environ.get("GROQ_API_KEY", "") if not self.path.exists() else "")

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, config: dict, key: str = "") -> None:
        # Explicit allowlist: credentials never belong in this JSON file.
        config = {name: config.get(name, "") for name in ("deck_path", "deck_name", "enable_hotkey")}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(config, stream, indent=2)
            if key:
                self.vault.set(key)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def discover_profiles(home: Path | None = None) -> list[dict]:
    root = (home or Path.home()) / "Library" / "Application Support" / "Anki2"
    if not root.exists():
        return []
    return [{"name": path.parent.name, "path": str(path)}
            for path in sorted(root.glob("*/collection.anki2"), key=lambda p: p.parent.name.casefold())
            if path.is_file()]
