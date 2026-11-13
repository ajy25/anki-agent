"""Per-user desktop preferences and a private, app-managed .env file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from dotenv import dotenv_values, set_key
from .providers import PROVIDERS, desktop_provider


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

    def get(self, provider: str = "groq") -> str:
        key_env = PROVIDERS[desktop_provider(provider)]["key_env"]
        try:
            if not self.path.exists():
                return ""
            # Treat the saved key literally, without expanding environment vars.
            return dotenv_values(self.path, interpolate=False).get(key_env) or ""
        except (OSError, UnicodeError):
            raise RuntimeError("Could not read the saved API key. Open Setup and save your key again.") from None

    def set(self, key: str, provider: str = "groq") -> None:
        self.set_many({desktop_provider(provider): key})

    def set_many(self, keys: dict[str, str]) -> None:
        entries = {PROVIDERS[desktop_provider(p)]["key_env"]: key for p, key in keys.items()}
        if any(not key or any(char.isspace() or char == "\x00" for char in key) for key in entries.values()):
            raise ValueError("Enter an API key without spaces or line breaks.")
        if not entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="credentials-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                if self.path.exists():
                    stream.write(self.path.read_text())
            # Preserve unrelated entries/comments if the user edited this file.
            for key_env, key in entries.items():
                set_key(temporary, key_env, key, quote_mode="always")
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

    def get_key(self, provider: str | None = None) -> str:
        provider = desktop_provider(provider or self.read().get("provider"))
        saved = self.vault.get() if provider == "groq" else self.vault.get(provider)
        # Source launches can supply an initial key via the environment. Once
        # Setup has been saved, a missing file must not revive a stale env key.
        return saved or (os.environ.get(PROVIDERS[provider]["key_env"], "") if not self.path.exists() else "")

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, config: dict, key: str = "", *, keys: dict[str, str] | None = None) -> None:
        provider = desktop_provider(config.get("provider"))
        # Explicit allowlist: credentials never belong in this JSON file.
        config = {name: config.get(name, "") for name in ("deck_path", "deck_name", "enable_hotkey")}
        config["provider"] = provider
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(config, stream, indent=2)
            if keys:
                self.vault.set_many(keys)
            elif key:
                if provider == "groq":
                    self.vault.set(key)
                else:
                    self.vault.set(key, provider)
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
