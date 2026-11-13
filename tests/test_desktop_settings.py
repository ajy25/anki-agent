"""Native menu routing checks without opening a window or starting hotkeys."""

import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    importlib.import_module("ankiagent.app")
with patch.dict("sys.modules", {"pynput": SimpleNamespace(
    keyboard=SimpleNamespace(GlobalHotKeys=type("FakeHotKeys", (), {}))
)}):
    launcher = importlib.import_module("launcher")


class DesktopSettingsTests(unittest.TestCase):
    def test_hide_and_hotkey_use_the_chat_history_preference(self):
        for hide in (launcher.Api().hide, launcher.toggle):
            with self.subTest(hide=hide), patch.object(launcher, "visible", True), patch.object(launcher, "window", Mock()) as window:
                hide()
                window.evaluate_js.assert_called_once_with(
                    "if(window.handleChatWindowClose){window.handleChatWindowClose();}"
                )
                window.hide.assert_called_once()
                self.assertFalse(launcher.visible)

    def test_native_model_action_focuses_current_provider_picker(self):
        settings, = launcher.settings_menu()
        self.assertEqual(settings.title, "Settings")
        self.assertEqual(settings.items[0].title, "API key & Anki deck…")
        action = settings.items[1]
        self.assertEqual(action.title, "AI model…")
        with patch.object(launcher, "window", Mock()) as window:
            action.function()
        window.evaluate_js.assert_called_once_with("document.getElementById('model-select')?.focus();")

    def test_invalid_selection_never_reaches_the_window(self):
        with patch.object(launcher, "window", Mock()) as window:
            with self.assertRaises(ValueError):
                launcher.select_model("invalid")
            window.evaluate_js.assert_not_called()

    def test_tutor_settings_menu_opens_the_shared_editor(self):
        settings, = launcher.settings_menu()
        action = next(item for item in settings.items if item.title == "Tutor settings…")
        with patch.object(launcher, "window", Mock()) as window:
            action.function()
        self.assertIn("window.openTutorSettings()", window.evaluate_js.call_args.args[0])

    def test_local_origin_is_remembered_between_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_server = SimpleNamespace(server_port=51337)
            with patch.object(launcher, "make_server", return_value=fake_server) as make:
                launcher.create_server(object(), root)
                self.assertEqual(make.call_args.args[1], 0)
                launcher.create_server(object(), root)
                self.assertEqual(make.call_args.args[1], 51337)

    def test_occupied_remembered_port_gets_a_working_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "desktop-port").write_text("51337")
            with patch.object(launcher, "make_server", side_effect=[SystemExit(1), SimpleNamespace(server_port=51338)]):
                launcher.create_server(object(), root)
            self.assertEqual((root / "desktop-port").read_text(), "51338")

if __name__ == "__main__":
    unittest.main()
