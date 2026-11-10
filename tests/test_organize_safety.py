"""Regression coverage for collection identity, reviewed selections, and recovery.

All Anki calls use stateful fakes. No real collection or provider is contacted.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_move_to_deck as move_tests
import test_suspend as suspend_tests
from ankiagent import anki_connect as anki, ingest, organize


class OrganizeSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.collection = self.root / "User 1" / "collection.anki2"
        self.collection.parent.mkdir()
        for p in (patch.dict(os.environ, {"ANKIAGENT_DATA_DIR": str(self.root / "app")}),
                  patch.object(ingest, "DECK_PATH", self.collection),
                  patch.dict(organize._previews, {}, clear=True),
                  patch.object(organize, "_sync_cache")):
            p.start()
            self.addCleanup(p.stop)
        self.client = move_tests.app_module.app.test_client()

    def connect(self, fake):
        p = patch.object(anki, "invoke", side_effect=fake.invoke)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def spec(self, action="suspend"):
        return organize.clean_card_spec({"action": action, "deck": suspend_tests.GI, "exclude_nids": [1]})

    def move(self, nids=None, target="GI"):
        nids = nids or [1]
        preview = organize.preview_move(nids, target)
        return organize.move_notes(nids, target, preview_id=preview["preview_id"])

    def test_wrong_profile_blocks_preview_status_and_picker(self):
        fake = self.connect(move_tests.FakeAnki())
        # Same profile basename under a different Anki data folder must fail.
        fake.media_path = str(self.root / "Other Anki folder" / "User 1" / "collection.media")
        for url, body in (("/api/move/preview", {"nids": [1], "target": "GI"}),
                          ("/api/cards/preview", self.spec())):
            response = self.client.post(url, json=body)
            self.assertEqual(response.status_code, 503)
            self.assertIn("different profile", response.get_json()["error"])
        for url in ("/api/anki/status", "/api/anki/decks"):
            self.assertEqual(self.client.get(url).status_code, 503)
        self.assertTrue(set(fake.calls) <= {"version", "getMediaDirPath"})

    def test_unverifiable_media_path_and_export_fail_closed(self):
        fake = self.connect(move_tests.FakeAnki())
        for media in (None, "", "collection.media"):
            fake.media_path = media
            with self.assertRaises(anki.AnkiConnectError):
                organize.preview_move([1], "GI")
        with patch.object(ingest, "DECK_PATH", self.root / "deck.txt"):
            with self.assertRaises(anki.AnkiConnectError):
                organize.preview_move([1], "GI")
        self.assertNotIn("changeDeck", fake.calls)

    def test_symlink_to_selected_profile_resolves_to_same_identity(self):
        fake = self.connect(move_tests.FakeAnki())
        alias = self.root / "profile-link"
        alias.symlink_to(self.collection.parent, target_is_directory=True)
        fake.media_path = str(alias / "collection.media")
        self.assertEqual(organize.preview_move([1], "GI")["cards_to_move"], 2)

    def test_shared_media_does_not_make_different_profiles_match(self):
        fake = self.connect(move_tests.FakeAnki())
        shared = self.root / "shared-media"
        shared.mkdir()
        other = self.root / "Other"
        other.mkdir()
        for profile in (self.collection.parent, other):
            (profile / "collection.media").symlink_to(shared, target_is_directory=True)
        fake.media_path = str(other / "collection.media")
        with self.assertRaises(anki.AnkiConnectError):
            organize.preview_move([1], "GI")

    def test_profile_switch_between_preview_and_apply_blocks_both_actions(self):
        for kind in ("move", "suspend"):
            with self.subTest(kind=kind):
                fake = move_tests.FakeAnki() if kind == "move" else suspend_tests.FakeAnki()
                with patch.object(anki, "invoke", side_effect=fake.invoke):
                    body = {"nids": [1], "target": "GI"} if kind == "move" else self.spec()
                    preview_url = "/api/move/preview" if kind == "move" else "/api/cards/preview"
                    apply_url = "/api/move" if kind == "move" else "/api/cards/apply"
                    body["preview_id"] = self.client.post(preview_url, json=body).get_json()["preview_id"]
                    fake.media_path = str(self.root / "Other" / "collection.media")
                    response = self.client.post(apply_url, json=body)
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn("changeDeck", fake.calls)
                    self.assertNotIn("suspend", fake.calls)

    def test_setup_switch_invalidates_preview_even_when_anki_matches_new_setup(self):
        fake = self.connect(move_tests.FakeAnki())
        preview = organize.preview_move([1], "GI")
        other = self.root / "Other" / "collection.anki2"
        fake.media_path = str(other.parent / "collection.media")
        with patch.object(ingest, "DECK_PATH", other):
            with self.assertRaises(organize.StalePreview):
                organize.move_notes([1], "GI", preview_id=preview["preview_id"])
        self.assertNotIn("changeDeck", fake.calls)

    def test_undo_is_bound_to_originating_collection_and_legacy_entries_fail_closed(self):
        fake = self.connect(move_tests.FakeAnki())
        moved = self.move()
        writes = fake.calls.count("changeDeck")
        fake.media_path = str(self.root / "Other" / "collection.media")
        with self.assertRaises(anki.AnkiConnectError):
            organize.undo_move(moved["id"])
        with patch.object(ingest, "DECK_PATH", self.root / "Other" / "collection.anki2"):
            self.assertEqual(organize.list_moves(), [])
            with self.assertRaises(ValueError):
                organize.undo_move(moved["id"])
        fake.media_path = str(self.collection.parent / "collection.media")
        entries = organize._read_history()
        del entries[0]["collection"]
        organize._write_history(entries)
        with self.assertRaisesRegex(ValueError, "no saved profile"):
            organize.undo_move(moved["id"])
        self.assertEqual(fake.calls.count("changeDeck"), writes)

    def test_write_routes_require_preview_and_token_is_single_use(self):
        fake = self.connect(move_tests.FakeAnki())
        body = {"nids": [1], "target": "GI"}
        self.assertEqual(self.client.post("/api/move", json=body).status_code, 409)
        preview = self.client.post("/api/move/preview", json=body).get_json()
        body["preview_id"] = preview["preview_id"]
        self.assertEqual(self.client.post("/api/move", json=body).status_code, 200)
        self.assertEqual(self.client.post("/api/move", json=body).status_code, 409)
        self.assertEqual(fake.calls.count("changeDeck"), 1)

    def test_card_route_requires_preview(self):
        fake = self.connect(suspend_tests.FakeAnki())
        response = self.client.post("/api/cards/apply", json=self.spec())
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["preview_required"])
        self.assertNotIn("suspend", fake.calls)

    def test_expired_and_modified_previews_cannot_be_applied(self):
        fake = self.connect(move_tests.FakeAnki())
        for mutation in ("expired", "target", "notes"):
            with self.subTest(mutation=mutation):
                preview = organize.preview_move([1], "GI")
                token = preview["preview_id"]
                if mutation == "expired":
                    organize._previews[token]["created"] -= organize.PREVIEW_TTL + 1
                with self.assertRaises(organize.StalePreview):
                    organize.move_notes([2] if mutation == "notes" else [1],
                                        "Other" if mutation == "target" else "GI", preview_id=token)
        self.assertNotIn("changeDeck", fake.calls)

    def test_new_cloze_or_changed_original_deck_requires_new_move_preview(self):
        for mutation in ("new_card", "moved_card"):
            with self.subTest(mutation=mutation):
                fake = move_tests.FakeAnki()
                with patch.object(anki, "invoke", side_effect=fake.invoke):
                    preview = organize.preview_move([1], "GI")
                    if mutation == "new_card":
                        fake.cards[13] = (1, "Study Deck::B&B")
                    else:
                        fake.cards[11] = (1, "Elsewhere")
                    with self.assertRaises(organize.StalePreview):
                        organize.move_notes([1], "GI", preview_id=preview["preview_id"])
                    self.assertNotIn("changeDeck", fake.calls)

    def test_added_or_replaced_cards_require_new_suspend_preview(self):
        for same_count in (False, True):
            with self.subTest(same_count=same_count):
                fake = suspend_tests.FakeAnki()
                with patch.object(anki, "invoke", side_effect=fake.invoke):
                    spec = self.spec()
                    preview = organize.preview_card_action(spec)
                    if same_count:
                        del fake.cards[201]
                    fake.cards[501] = [5, suspend_tests.GI, False]
                    with self.assertRaises(organize.StalePreview):
                        organize.apply_card_action(spec, preview_id=preview["preview_id"])
                    self.assertFalse(fake.cards[501][2])
                    self.assertNotIn("suspend", fake.calls)

    def test_card_action_token_cannot_change_action_or_exceptions(self):
        fake = self.connect(suspend_tests.FakeAnki())
        for field, value in (("action", "unsuspend"), ("exclude_nids", [])):
            spec = self.spec()
            preview = organize.preview_card_action(spec)
            spec[field] = value
            with self.assertRaises(organize.StalePreview):
                organize.apply_card_action(spec, preview_id=preview["preview_id"])
        self.assertNotIn("suspend", fake.calls)
        self.assertNotIn("unsuspend", fake.calls)

    def test_lost_move_response_preserves_originals_and_allows_undo(self):
        fake = self.connect(move_tests.FakeAnki())
        original = dict(fake.cards)
        preview = organize.preview_move([1], "GI")
        def lost_response(action, **params):
            result = fake.invoke(action, **params)
            if action == "changeDeck":
                raise TimeoutError("response lost after commit")
            return result
        with patch.object(anki, "invoke", side_effect=lost_response):
            response = self.client.post("/api/move", json={"nids": [1], "target": "GI",
                                                         "preview_id": preview["preview_id"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("recovery record", response.get_json()["error"])
        self.assertNotIn("Nothing was changed", response.get_json()["error"])
        self.assertEqual(fake.deck_of(11), "GI")
        entry = organize._read_history()[-1]
        self.assertEqual(entry["status"], "uncertain")
        self.assertEqual(entry["collection"], str(self.collection))
        self.assertEqual(entry["cards"][0], [11, 1, original[11][1]])
        organize.undo_move(entry["id"])
        self.assertEqual(fake.cards, original)

    def test_partial_suspend_or_unsuspend_is_recoverable(self):
        for action in ("suspend", "unsuspend"):
            with self.subTest(action=action):
                fake = suspend_tests.FakeAnki()
                for card in fake.cards.values():
                    card[2] = action == "unsuspend"
                original = {c: list(v) for c, v in fake.cards.items()}
                with patch.object(anki, "invoke", side_effect=fake.invoke):
                    spec = organize.clean_card_spec({"action": action, "deck": suspend_tests.GI})
                    preview = organize.preview_card_action(spec)
                    def partial(action_name, **params):
                        if action_name == action:
                            fake.invoke(action_name, cards=params["cards"][:1])
                            raise TimeoutError("partial write")
                        return fake.invoke(action_name, **params)
                    with patch.object(anki, "invoke", side_effect=partial):
                        with self.assertRaises(organize.ChangeUncertain):
                            organize.apply_card_action(spec, preview_id=preview["preview_id"])
                    entry = organize._read_history()[-1]
                    self.assertEqual(entry["status"], "uncertain")
                    organize.undo_move(entry["id"])
                    self.assertEqual(fake.cards, original)

    def test_failed_completion_log_update_keeps_pending_recovery_record(self):
        fake = self.connect(move_tests.FakeAnki())
        with patch.object(organize, "_update_entry", side_effect=OSError("disk full")):
            with self.assertRaises(organize.ChangeUncertain):
                self.move()
        entry = organize._read_history()[-1]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(fake.deck_of(11), "GI")
        organize.undo_move(entry["id"])
        self.assertEqual(fake.deck_of(11), "Study Deck::B&B")

    def test_unsuspend_null_response_is_verified_against_actual_state(self):
        fake = self.connect(suspend_tests.FakeAnki())
        def null_response(action, **params):
            result = fake.invoke(action, **params)
            return None if action == "unsuspend" else result
        with patch.object(anki, "invoke", side_effect=null_response):
            spec = self.spec("unsuspend")
            preview = organize.preview_card_action(spec)
            result = organize.apply_card_action(spec, preview_id=preview["preview_id"])
        self.assertEqual(result["changed"], 1)
        self.assertFalse(fake.cards[301][2])
        self.assertEqual(organize._read_history()[-1]["status"], "done")

    def test_unsuccessful_card_state_verification_preserves_recovery(self):
        fake = self.connect(suspend_tests.FakeAnki())
        spec = self.spec()
        preview = organize.preview_card_action(spec)
        def no_change(action, **params):
            return False if action == "suspend" else fake.invoke(action, **params)
        with patch.object(anki, "invoke", side_effect=no_change):
            with self.assertRaises(organize.ChangeUncertain):
                organize.apply_card_action(spec, preview_id=preview["preview_id"])
        self.assertEqual(organize._read_history()[-1]["status"], "uncertain")

    def test_failed_undo_keeps_record_for_safe_retry(self):
        fake = self.connect(move_tests.FakeAnki())
        moved = self.move([1, 2])
        def partial(action, **params):
            result = fake.invoke(action, **params)
            if action == "changeDeck":
                raise TimeoutError("undo response lost")
            return result
        with patch.object(anki, "invoke", side_effect=partial):
            with self.assertRaises(organize.ChangeUncertain):
                organize.undo_move(moved["id"])
        self.assertEqual(organize._read_history()[-1]["status"], "uncertain")
        organize.undo_move(moved["id"])
        self.assertEqual(fake.deck_of(11), "Study Deck::B&B")
        self.assertEqual(fake.deck_of(21), "Study Deck::Pathoma")

    def test_uncertain_history_is_not_pruned_or_hidden_by_new_changes(self):
        pending = {"id": "recover", "collection": str(self.collection), "status": "uncertain", "cards": []}
        entries = [pending] + [{"id": str(i), "collection": str(self.collection), "status": "done", "cards": []}
                               for i in range(organize.HISTORY_LIMIT + 5)]
        organize._write_history(entries)
        self.assertEqual(len(organize._read_history()), organize.HISTORY_LIMIT + 1)
        self.assertIn("recover", [e["id"] for e in organize.list_moves()])


if __name__ == "__main__":
    unittest.main()
