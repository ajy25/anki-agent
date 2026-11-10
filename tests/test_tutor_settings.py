"""Persistent Tutor preferences and their use in actual model requests."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")
from ankiagent import llm, tutor_settings


class TutorSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"ANKIAGENT_DATA_DIR": temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def test_defaults_round_trip_and_desktop_settings_are_preserved(self):
        desktop = self.root / "settings.json"
        desktop.write_text('{"deck_name": "Study"}')
        original = desktop.read_bytes()
        response = self.client.get("/api/tutor/settings")
        self.assertEqual(response.get_json()["settings"], tutor_settings.default_prompts())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        custom = {"system_prompt": "Teach with examples.\nUse short paragraphs.", "first_turn_format": ""}
        response = self.client.post("/api/tutor/settings", json=custom)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tutor_settings.read_prompts(), custom)
        self.assertEqual(self.client.get("/api/tutor/settings").get_json()["settings"], custom)
        self.assertEqual(desktop.read_bytes(), original)
        self.client.post("/api/tutor/settings", json=tutor_settings.default_prompts())
        self.assertEqual(tutor_settings.read_prompts(), tutor_settings.default_prompts())

    def test_invalid_and_cross_site_writes_cannot_replace_saved_instructions(self):
        original = {"system_prompt": "Saved prompt", "first_turn_format": ""}
        tutor_settings.save_prompts(original)
        for value in (None, [], {}, {"system_prompt": "", "first_turn_format": ""},
                      {"system_prompt": "x" * 20001, "first_turn_format": ""},
                      {"system_prompt": "Valid", "first_turn_format": 1}):
            with self.subTest(value=type(value)):
                response = self.client.post("/api/tutor/settings", json=value, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/api/tutor/settings", json=original,
                         headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/tutor/settings", data=original).status_code, 415)
        self.assertEqual(tutor_settings.read_prompts(), original)

    def test_corrupt_preferences_fall_back_to_defaults(self):
        (self.root / "tutor-settings.json").write_text("not valid JSON")
        self.assertEqual(tutor_settings.read_prompts(), tutor_settings.default_prompts())

    def test_custom_instructions_reach_tutor_but_not_organize(self):
        custom = {"system_prompt": "Teach with diagrams.", "first_turn_format": "Use one paragraph."}
        tutor_settings.save_prompts(custom)
        with patch.object(llm, "_chat_with_retries", return_value=("", [], {})) as chat:
            llm.tutor_chat([{"role": "user", "content": "Explain this card"}])
            self.assertEqual(chat.call_args.kwargs["system_prompt"], custom["system_prompt"])
            self.assertEqual(chat.call_args.kwargs["first_turn_format"], custom["first_turn_format"])
            self.assertIs(chat.call_args.kwargs["tools"], llm.TUTOR_TOOLS)
            llm.agent_chat([{"role": "user", "content": "Move cards"}])
            self.assertIn(llm.AGENT_ORGANIZER_SYSTEM, chat.call_args.kwargs["system_prompt"])
            self.assertNotIn(custom["system_prompt"], chat.call_args.kwargs["system_prompt"])
