"""Per-user desktop preferences and native macOS credential storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


def data_dir() -> Path:
    override = os.environ.get("ANKIAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Anki Agent"
    return Path.home() / ".local" / "share" / "anki-agent"


class MacKeychain:
    service = "org.ankiagent.desktop"
    account = "groq-api-key"

    def _query(self):
        import Security
        return {Security.kSecClass: Security.kSecClassGenericPassword,
                Security.kSecAttrService: self.service, Security.kSecAttrAccount: self.account}

    def get(self) -> str:
        import Security
        query = {**self._query(), Security.kSecReturnData: True,
                 Security.kSecMatchLimit: Security.kSecMatchLimitOne}
        status, value = Security.SecItemCopyMatching(query, None)
        if status == Security.errSecItemNotFound:
            return ""
        if status != Security.errSecSuccess:
            raise RuntimeError(f"macOS Keychain could not read the API key (code {status}).")
        return bytes(value).decode("utf-8")

    def set(self, key: str) -> None:
        import Security
        values = {Security.kSecValueData: key.encode("utf-8")}
        status = Security.SecItemUpdate(self._query(), values)
        if status == Security.errSecItemNotFound:
            status, _ = Security.SecItemAdd({**self._query(), **values,
                Security.kSecAttrAccessible: Security.kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly}, None)
        if status != Security.errSecSuccess:
            raise RuntimeError(f"macOS Keychain could not save the API key (code {status}).")


class SettingsStore:
    def __init__(self, root: Path | None = None, vault=None):
        self.root = root or data_dir()
        self.path = self.root / "settings.json"
        self.vault = vault or MacKeychain()

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
