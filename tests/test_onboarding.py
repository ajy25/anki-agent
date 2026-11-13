"""First-run setup without real credentials, user decks, or network calls."""
from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from flask import Flask
from werkzeug.test import Client
from werkzeug.wrappers import Response
from ankiagent.desktop import DesktopManager, collection_decks, create_desktop_app, validate_key
from ankiagent.settings import EnvCredentials, SettingsStore, discover_profiles


class Vault:
    def __init__(self, key=""): self.key = key
    def get(self, provider="groq"): return self.key if provider == "groq" else ""
    def set(self, key, provider="groq"): self.key = key
    def set_many(self, keys):
        if "groq" in keys: self.key = keys["groq"]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.collection = self.root / "collection.anki2"
        with closing(sqlite3.connect(self.collection)) as connection, connection:
            connection.execute("CREATE TABLE decks (id INTEGER, name TEXT)")
            connection.executemany("INSERT INTO decks VALUES (?, ?)", [(1,"Default"),(2,"Study"),(3,"Study\x1fGI")])
        self.vault = Vault()
        self.store = SettingsStore(self.root / "preferences", self.vault)
        env = patch.dict(os.environ, {"GROQ_API_KEY": "", "DECK_PATH": "", "HOTKEY": ""})
        env.start(); self.addCleanup(env.stop)
        profiles = patch("ankiagent.desktop.discover_profiles", return_value=[])
        profiles.start(); self.addCleanup(profiles.stop)
        self.backend = Flask("sample-backend")
        self.backend.add_url_rule("/", view_func=lambda: "Your deck is ready")
        self.factory = Mock(return_value=self.backend)
        self.manager = DesktopManager(self.store, self.factory)
        application, self.setup = create_desktop_app(self.manager, key_validator=lambda key, provider: None)
        self.client = Client(application, Response)
        self.headers = {"X-AnkiAgent-Setup-Token": self.manager.token}

    def post(self, route, data):
        return self.client.post("/api/setup/" + route, json=data, headers=self.headers,
                                base_url="http://127.0.0.1")

    def test_fresh_launch_shows_setup_without_requiring_env_or_a_key(self):
        result = self.client.get("/", base_url="http://127.0.0.1")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Set up Anki Agent", result.data)
        self.assertIn(b"Save &amp; open deck", result.data)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.factory.assert_not_called()

    def test_setup_api_rejects_cross_origin_requests_and_never_returns_a_key(self):
        self.vault.key = "gsk_private_test_value"
        self.assertEqual(self.client.get("/api/setup/state", base_url="http://127.0.0.1").status_code, 403)
        result = self.client.get("/api/setup/state", headers=self.headers, base_url="http://127.0.0.1")
        self.assertNotIn(b"gsk_private_test_value", result.data)
        self.assertEqual(self.client.get("/setup", base_url="http://evil.example").status_code, 403)

    def test_profiles_detect_anki_and_deck_picker_includes_nested_decks(self):
        profile = self.root / "Library" / "Application Support" / "Anki2" / "User 1"
        profile.mkdir(parents=True)
        (profile / "collection.anki2").write_bytes(self.collection.read_bytes())
        self.assertEqual(discover_profiles(self.root)[0]["name"], "User 1")
        result = self.post("decks", {"path": str(self.collection)})
        self.assertEqual(result.get_json()["decks"], ["Default", "Study", "Study::GI"])

    def test_save_stores_key_only_in_vault_and_starts_background_preparation(self):
        with patch.object(self.manager, "prepare") as prepare:
            result = self.post("save", {"path": str(self.collection), "deck": "Study::GI", "key": "gsk_test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.vault.key, "gsk_test")
        self.assertNotIn("gsk_test", self.store.path.read_text())
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(self.store.read()["deck_name"], "Study::GI")
        prepare.assert_called_once()

    def test_saved_key_is_kept_when_changing_decks(self):
        self.store.save({"deck_path": str(self.collection), "deck_name": "Study"}, "gsk_existing")
        with patch.object(self.manager, "prepare"):
            result = self.post("save", {"path": str(self.collection), "deck": "Study::GI", "key": ""})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.vault.key, "gsk_existing")

    def test_invalid_decks_and_packages_fail_before_saving_key(self):
        result = self.post("save", {"path": str(self.collection), "deck": "Missing", "key": "gsk_test"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.vault.key, "")
        package = self.root / "export.apkg"; package.write_bytes(b"export")
        with self.assertRaisesRegex(ValueError, "exported package"):
            collection_decks(str(package))

    def test_background_preparation_returns_to_app_and_setup_remains_accessible(self):
        self.store.save({"deck_path": str(self.collection), "deck_name": "Study"}, "gsk_test")
        self.manager.config = self.store.read()
        self.manager._prepare()
        self.assertEqual(self.manager.status, "ready")
        self.assertEqual(self.client.get("/", base_url="http://127.0.0.1").data, b"Your deck is ready")
        self.assertIn(b"Set up Anki Agent", self.client.get("/setup", base_url="http://127.0.0.1").data)

    def test_indexing_screen_is_responsive_while_backend_holds_configuration_lock(self):
        self.manager._set_status("indexing")
        with self.manager.lock:
            result = self.client.get("/", base_url="http://127.0.0.1")
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Set up Anki Agent", result.data)

    def test_key_validation_does_not_leak_provider_error_details(self):
        fake = MagicMock()
        fake.return_value.__enter__.return_value.models.list.side_effect = RuntimeError("gsk_do_not_echo")
        with patch("groq.Groq", fake):
            with self.assertRaisesRegex(ValueError, "Could not reach Groq") as error:
                validate_key("gsk_do_not_echo")
        self.assertNotIn("gsk_do_not_echo", str(error.exception))

    def test_real_indexing_and_deck_switching_with_synthetic_collection(self):
        report = self.root / "report.json"
        environment = {**os.environ, "ANKIAGENT_DATA_DIR": str(self.root / "fresh-user")}
        subprocess.run([sys.executable, "desktop_entry.py", "--smoke-test", "--report", str(report)],
                       cwd=Path(__file__).resolve().parents[1], env=environment, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        result = json.loads(report.read_text())
        self.assertTrue(result["deck_switching"])
        self.assertTrue(result["indexing"])
        self.assertTrue(result["source_read_only"])

    def test_default_store_saves_private_env_and_reopens_without_keychain(self):
        store = SettingsStore(self.root / "local-settings")
        with patch.dict(sys.modules, {"Security": None}):
            self.assertIsInstance(store.vault, EnvCredentials)
            self.assertEqual(store.get_key(), "")
            store.save({"deck_path": str(self.collection), "deck_name": "Study"}, "gsk_fixture")
            reopened = SettingsStore(store.root)
            self.assertEqual(reopened.get_key(), "gsk_fixture")
            manager = DesktopManager(reopened, self.factory)
            manager._prepare()
            self.assertEqual(manager.status, "ready")
        self.assertEqual(stat.S_IMODE(store.vault.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
        self.assertNotIn("gsk_fixture", store.path.read_text())

    def test_env_update_preserves_other_entries_and_treats_key_literally(self):
        vault = EnvCredentials(self.root)
        vault.path.write_text('# Keep this comment\nOTHER_SETTING="value"\nGROQ_API_KEY=old\n')
        vault.path.chmod(0o644)
        vault.set("gsk_${OTHER_SETTING}")
        self.assertEqual(vault.get(), "gsk_${OTHER_SETTING}")
        self.assertIn('# Keep this comment\nOTHER_SETTING="value"', vault.path.read_text())
        self.assertEqual(stat.S_IMODE(vault.path.stat().st_mode), 0o600)

    def test_failed_env_write_keeps_old_key_and_removes_temporary_file(self):
        vault = EnvCredentials(self.root)
        vault.set("gsk_old")
        with patch("ankiagent.settings.os.replace", side_effect=OSError("gsk_never_echo")):
            with self.assertRaisesRegex(RuntimeError, "Could not save") as error:
                vault.set("gsk_new")
        self.assertNotIn("gsk_never_echo", str(error.exception))
        self.assertEqual(vault.get(), "gsk_old")
        self.assertEqual(list(self.root.glob("credentials-*")), [])

    def test_env_credentials_reject_multiline_keys_without_overwriting(self):
        vault = EnvCredentials(self.root)
        vault.set("gsk_original")
        for key in ("", "gsk_a\nOTHER=x", "gsk_a b", "gsk_a\x00b"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                vault.set(key)
        self.assertEqual(vault.get(), "gsk_original")

    def test_local_env_works_before_settings_and_missing_saved_key_ignores_stale_environment(self):
        store = SettingsStore(self.root / "local-settings")
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_environment"}):
            self.assertEqual(store.get_key(), "gsk_environment")
            store.vault.set("gsk_file")
            self.assertEqual(store.get_key(), "gsk_file")
            store.save({})
            store.vault.path.unlink()
            self.assertEqual(store.get_key(), "")

    def test_setup_can_replace_an_unreadable_old_key(self):
        with patch.object(self.vault, "get", side_effect=RuntimeError("Unreadable key")), patch.object(self.manager, "prepare"):
            result = self.post("save", {"path": str(self.collection), "deck": "Study", "key": "gsk_replacement"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.vault.key, "gsk_replacement")

    def test_setup_uses_real_env_file_without_returning_or_serving_it(self):
        store = SettingsStore(self.root / "local-settings")
        manager = DesktopManager(store, self.factory)
        application, _ = create_desktop_app(manager, key_validator=lambda key, provider: None)
        client = Client(application, Response)
        headers = {"X-AnkiAgent-Setup-Token": manager.token}
        with patch.object(manager, "prepare"):
            result = client.post("/api/setup/save", json={"path": str(self.collection), "deck": "Study", "key": "gsk_private_fixture"},
                                 headers=headers, base_url="http://127.0.0.1")
            self.assertEqual(result.status_code, 200)
            result = client.post("/api/setup/save", json={"path": str(self.collection), "deck": "Study::GI", "key": ""},
                                 headers=headers, base_url="http://127.0.0.1")
            self.assertEqual(result.status_code, 200)
        self.assertEqual(store.get_key(), "gsk_private_fixture")
        for url in ("/api/setup/state", "/setup", "/.env", "/setup-assets/.env"):
            response = client.get(url, headers=headers, base_url="http://127.0.0.1")
            self.assertNotIn(b"gsk_private_fixture", response.data)

    def test_saving_with_shortcut_enabled_keeps_native_work_off_setup_worker(self):
        launcher = importlib.import_module("launcher")
        queued, notified = [], threading.Event()
        def schedule(callback):
            queued.append(callback)
            notified.set()
        self.manager.on_ready = launcher.update_hotkey
        listener = Mock()
        with patch.object(launcher, "manager", self.manager), \
             patch.object(launcher, "hotkey_listener", None), \
             patch.object(launcher, "_schedule_on_main", side_effect=schedule), \
             patch.object(launcher, "NativeHotkey", return_value=listener) as factory:
            response = self.post("save", {"path": str(self.collection), "deck": "Study",
                "key": "gsk_fixture", "enable_hotkey": True})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(notified.wait(2))
            self.assertEqual(self.manager.status, "ready")
            factory.assert_not_called()
            queued[0]()
            listener.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
