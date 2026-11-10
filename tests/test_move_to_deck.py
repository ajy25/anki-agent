"""Moving cards between decks: preview, move, undo log, and route guards.

AnkiConnect is replaced by an in-memory fake so no real Anki is touched.
"""

import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import anki_connect, ingest, organize, search
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")


class FakeAnki:
    """Just enough of AnkiConnect: notes own cards, cards live in decks."""

    def __init__(self):
        self.cards = {  # card id -> (note id, deck)
            11: (1, "Study Deck::B&B"), 12: (1, "Study Deck::B&B"),
            21: (2, "Study Deck::Pathoma"),
            31: (3, "GI"),
        }
        self.decks = {"Study Deck", "Study Deck::B&B", "Study Deck::Pathoma", "GI"}
        self.calls = []
        self.media_path = str(ingest.DECK_PATH.parent / "collection.media")

    def invoke(self, action, **p):
        self.calls.append(action)
        if action == "version":
            return 6
        if action == "getMediaDirPath":
            return self.media_path
        if action == "deckNames":
            return sorted(self.decks)
        if action == "notesInfo":
            out = []
            for nid in p["notes"]:
                cids = [c for c, (n, _) in self.cards.items() if n == nid]
                out.append({"noteId": nid, "cards": cids} if cids else {})
            return out
        if action == "getDecks":
            grouped = {}
            for c in p["cards"]:
                if c in self.cards:
                    grouped.setdefault(self.cards[c][1], []).append(c)
            return grouped
        if action == "createDeck":
            self.decks.add(p["deck"])
            return 99
        if action == "changeDeck":
            self.decks.add(p["deck"])
            for c in p["cards"]:
                self.cards[c] = (self.cards[c][0], p["deck"])
            return None
        raise AssertionError(f"unexpected action {action}")

    def deck_of(self, cid):
        return self.cards[cid][1]


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAnki()
        p = patch.object(anki_connect, "invoke", side_effect=self.fake.invoke)
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"ANKIAGENT_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        scope = patch.object(ingest, "DECK_NAME", "Study Deck")
        scope.start()
        self.addCleanup(scope.stop)

        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(ingest.SCHEMA)
        for guid, nid, deck in (("g1", 1, "Study Deck::B&B"), ("g2", 2, "Study Deck::Pathoma")):
            self.con.execute(
                "INSERT INTO notes (guid, front, back, front_html, back_html, extras, tags_raw, deck, nid) "
                "VALUES (?, 'f', 'b', '', '', '[]', '', ?, ?)", (guid, deck, nid))
            self.con.execute("INSERT INTO note_decks VALUES (?, ?)", (guid, deck))
        conn = patch.object(search, "get_conn", return_value=self.con)
        conn.start()
        self.addCleanup(conn.stop)
        self.client = app_module.app.test_client()

    def post(self, url, body=None, **kw):
        if url == "/api/move" and body and "preview_id" not in body:
            preview = self.client.post("/api/move/preview", json=body)
            if preview.status_code == 200:
                body = {**body, "preview_id": preview.get_json()["preview_id"]}
        return self.client.post(url, json=body or {}, **kw)

    # --- validation -------------------------------------------------------

    def test_deck_names_are_normalized_and_validated(self):
        self.assertEqual(organize.clean_deck_name("  Step 1 :: GI "), "Step 1::GI")
        for bad in ("", "   ", "GI::", "::GI", "A::::B", "G\nI", None, 5):
            with self.assertRaises(ValueError):
                organize.clean_deck_name(bad)

    def test_nids_are_validated(self):
        self.assertEqual(organize.clean_nids([3, "2", 2]), [2, 3])
        for bad in ([], None, ["x"], [0, -1]):
            with self.assertRaises(ValueError):
                organize.clean_nids(bad)

    # --- preview ------------------------------------------------------------

    def test_preview_counts_cards_not_notes_and_changes_nothing(self):
        r = self.post("/api/move/preview", {"nids": [1, 2, 3, 404], "target": "GI"})
        self.assertEqual(r.status_code, 200, r.get_json())
        p = r.get_json()
        self.assertEqual(p["notes"], 3)
        self.assertEqual(p["missing_notes"], 1)
        self.assertEqual(p["cards_to_move"], 3)        # two cloze cards + one
        self.assertEqual(p["already_in_target"], 1)    # card 31 is in GI already
        self.assertTrue(p["target_exists"])
        self.assertTrue(p["outside_index_scope"])
        self.assertEqual({d["deck"]: d["cards"] for d in p["from_decks"]},
                         {"Study Deck::B&B": 2, "Study Deck::Pathoma": 1})
        self.assertNotIn("changeDeck", self.fake.calls)

    def test_subdeck_of_indexed_deck_is_a_valid_target_and_stays_searchable(self):
        p = self.post("/api/move/preview", {"nids": [1, 2], "target": "Study Deck :: GI"}).get_json()
        self.assertEqual(p["target"], "Study Deck::GI")
        self.assertFalse(p["target_exists"])
        self.assertFalse(p["outside_index_scope"])
        moved = self.post("/api/move", {"nids": [1, 2], "target": "Study Deck::GI"}).get_json()
        self.assertEqual(moved["moved"], 3)
        self.assertEqual(self.fake.deck_of(21), "Study Deck::GI")
        undo = self.post(f"/api/moves/{moved['id']}/undo").get_json()
        self.assertEqual(undo["restored"], 3)
        self.assertEqual(self.fake.deck_of(11), "Study Deck::B&B")

    # --- move + undo ----------------------------------------------------------

    def test_move_then_undo_restores_original_decks(self):
        r = self.post("/api/move", {"nids": [1, 2], "target": "Step 1::GI", "label": "B&B GI"})
        self.assertEqual(r.status_code, 200, r.get_json())
        moved = r.get_json()
        self.assertEqual(moved["moved"], 3)
        self.assertIn("createDeck", self.fake.calls)
        self.assertEqual({self.fake.deck_of(c) for c in (11, 12, 21)}, {"Step 1::GI"})
        # The search cache follows the move.
        decks = {r["guid"]: r["deck"] for r in self.con.execute("SELECT * FROM note_decks")}
        self.assertEqual(decks, {"g1": "Step 1::GI", "g2": "Step 1::GI"})

        history = self.client.get("/api/moves").get_json()["moves"]
        self.assertEqual(history[0]["status"], "done")
        self.assertEqual(history[0]["cards"], 3)

        undo = self.post(f"/api/moves/{moved['id']}/undo").get_json()
        self.assertEqual(undo["restored"], 3)
        self.assertEqual(self.fake.deck_of(11), "Study Deck::B&B")
        self.assertEqual(self.fake.deck_of(21), "Study Deck::Pathoma")
        decks = {r["guid"]: r["deck"] for r in self.con.execute("SELECT * FROM note_decks")}
        self.assertEqual(decks["g1"], "Study Deck::B&B")
        self.assertEqual(self.client.get("/api/moves").get_json()["moves"][0]["status"], "undone")

        again = self.post(f"/api/moves/{moved['id']}/undo")
        self.assertEqual(again.status_code, 400)

    def test_undo_leaves_cards_that_were_moved_again(self):
        preview = organize.preview_move([1], "GI")
        moved = organize.move_notes([1], "GI", preview_id=preview["preview_id"])
        self.fake.cards[12] = (1, "Somewhere else")     # user moved it by hand
        undo = organize.undo_move(moved["id"])
        self.assertEqual((undo["restored"], undo["skipped"]), (1, 1))
        self.assertEqual(self.fake.deck_of(11), "Study Deck::B&B")
        self.assertEqual(self.fake.deck_of(12), "Somewhere else")

    def test_history_is_written_before_anki_changes(self):
        def fail_on_change(action, **p):
            if action == "changeDeck":
                self.assertEqual(json.loads(organize.history_path().read_text())[0]["status"], "pending")
                raise anki_connect.AnkiConnectError("boom")
            return self.fake.invoke(action, **p)
        with patch.object(anki_connect, "invoke", side_effect=fail_on_change):
            r = self.post("/api/move", {"nids": [1], "target": "GI"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(json.loads(organize.history_path().read_text())[0]["status"], "uncertain")

    def test_already_in_target_is_a_no_op(self):
        r = self.post("/api/move", {"nids": [3], "target": "GI"}).get_json()
        self.assertEqual((r["moved"], r["id"]), (0, None))
        self.assertNotIn("changeDeck", self.fake.calls)

    # --- errors and guards --------------------------------------------------

    def test_connection_status_checks_selected_collection_with_short_timeout(self):
        with patch.object(anki_connect, "check", return_value=6) as check:
            r = self.client.get("/api/anki/status")
        check.assert_called_once_with(timeout=3.0, collection_path=ingest.DECK_PATH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"connected": True, "version": 6})
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(self.fake.calls, [])

    def test_connection_status_reports_unavailable_and_outdated_addons(self):
        for version in (None, 5):
            with self.subTest(version=version):
                with patch.object(anki_connect, "invoke", return_value=version,
                                  side_effect=anki_connect.AnkiConnectError(anki_connect.NOT_RUNNING)
                                  if version is None else None):
                    r = self.client.get("/api/anki/status")
                self.assertEqual(r.status_code, 503)
                self.assertTrue(r.get_json()["anki_unavailable"])
                self.assertIn("AnkiConnect", r.get_json()["error"])
                self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_anki_not_running_is_reported_clearly(self):
        with patch.object(anki_connect, "invoke", side_effect=anki_connect.AnkiConnectError(anki_connect.NOT_RUNNING)):
            r = self.post("/api/move/preview", {"nids": [1], "target": "GI"})
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.get_json()["anki_unavailable"])
        self.assertIn("AnkiConnect", r.get_json()["error"])

    def test_write_routes_refuse_cross_site_and_form_posts(self):
        cross = self.post("/api/move", {"nids": [1], "target": "GI"},
                          headers={"Origin": "https://evil.example"})
        self.assertEqual(cross.status_code, 403)
        form = self.client.post("/api/move", data={"nids": "1", "target": "GI"})
        self.assertEqual(form.status_code, 415)
        same = self.post("/api/move/preview", {"nids": [1], "target": "GI"},
                         headers={"Origin": "http://localhost"})
        self.assertEqual(same.status_code, 200)
        self.assertNotIn("changeDeck", self.fake.calls)


class ResolveTargetTests(unittest.TestCase):
    DECKS = ["Study Deck", "Study Deck::GI", "Study Deck::Renal",
             "Step 2::Renal", "Default"]

    def test_exact_path_case_insensitive(self):
        r = organize.resolve_target("study deck :: gi", self.DECKS)
        self.assertEqual((r["target"], r["exists"]), ("Study Deck::GI", True))

    def test_unique_leaf_name_matches_subdeck(self):
        r = organize.resolve_target("gi", self.DECKS)
        self.assertEqual((r["target"], r["exists"]), ("Study Deck::GI", True))

    def test_ambiguous_leaf_becomes_new_deck_with_suggestions(self):
        r = organize.resolve_target("Renal", self.DECKS)
        self.assertEqual((r["target"], r["exists"]), ("Renal", False))
        self.assertIn("Study Deck::Renal", r["suggestions"])
        self.assertIn("Step 2::Renal", r["suggestions"])

    def test_new_subdeck_does_not_suggest_its_own_parent(self):
        r = organize.resolve_target("Study Deck::Cardio", self.DECKS)
        self.assertEqual((r["target"], r["exists"]), ("Study Deck::Cardio", False))
        self.assertNotIn("Study Deck", r["suggestions"])

    def test_deck_typed_verbatim_in_message_beats_shortened_model_name(self):
        decks = self.DECKS + ["Study Deck::GI cards"]
        msg = "Put all Boards and Beyond GI cards in Study Deck::GI cards"
        r = organize.resolve_target("Study Deck::GI", decks, msg)
        self.assertEqual((r["target"], r["exists"]), ("Study Deck::GI cards", True))
        # Without that deck in the message, the model's name stands.
        r = organize.resolve_target("Study Deck::GI", decks, "move B&B GI to my GI deck")
        self.assertEqual(r["target"], "Study Deck::GI")

    def test_unknown_deck_is_new(self):
        r = organize.resolve_target("Cardio", self.DECKS)
        self.assertEqual((r["target"], r["exists"]), ("Cardio", False))


class AgentProposeMoveTests(unittest.TestCase):
    """The chat agent prepares moves; it can never perform them."""

    def setUp(self):
        from ankiagent import llm
        self.llm = llm
        self.fake = FakeAnki()
        for target, kw in ((anki_connect, {"side_effect": self.fake.invoke}),):
            p = patch.object(target, "invoke", **kw)
            p.start()
            self.addCleanup(p.stop)
        hits = [{"guid": "g1", "nid": 1, "front": "Achalasia", "back": "", "tags": ["#Study_Step1::#B&B::10_GI"]},
                {"guid": "g2", "nid": 2, "front": "Barrett", "back": "", "tags": []}]
        self.search = patch.object(llm, "assisted_search",
                                   return_value=(hits, {"tag_filters": ["#B&B::10_GI"], "explanation": "B&B GI tag"}, {}))
        self.search_mock = self.search.start()
        self.addCleanup(self.search.stop)
        # These tests cover the AI card finder; the tag shortcut has its own tests.
        direct = patch.object(llm, "direct_tag_selection", return_value=None)
        direct.start()
        self.addCleanup(direct.stop)

    def session(self, **kw):
        return self.llm.AgentSession("move B&B GI to my GI deck", tools=self.llm.AGENT_ORGANIZER_TOOLS, **kw)

    def test_only_anki_agent_gets_the_write_tools(self):
        names = [t["function"]["name"] for t in self.llm.AGENT_ORGANIZER_TOOLS]
        self.assertEqual(names, ["search_deck", "read_note", "propose_move", "propose_card_action",
                                 "propose_undo"])
        # Tutor keeps its original, read-only tools and forced first search.
        self.assertEqual([t["function"]["name"] for t in self.llm.TUTOR_TOOLS], ["search_deck", "read_note"])
        tutor = self.llm.AgentSession("q", tools=self.llm.TUTOR_TOOLS, require_deck_search=True)
        self.assertEqual(tutor._tool_choice(0)["function"]["name"], "search_deck")
        self.assertEqual(tutor._dispatch("propose_move", {"selection": "x", "target_deck": "GI"})["error"],
                         "unknown_tool:propose_move")
        self.assertNotIn("propose_", self.llm.TUTOR_AGENT_SYSTEM)

    def test_proposal_is_recorded_but_nothing_moves(self):
        s = self.session(deck="Study Deck")
        out = s._dispatch("propose_move", {"selection": "Boards and Beyond GI cards", "target_deck": "gi"})
        self.assertEqual(out["notes_matched"], 2)
        self.assertEqual(out["target_deck"], "GI")
        self.assertTrue(out["target_exists"])
        self.assertNotIn("nids", out)                        # ids stay out of the prompt
        self.assertEqual(s.proposals[0]["nids"], [1, 2])
        self.assertEqual(self.search_mock.call_args.kwargs["deck"], "Study Deck")
        self.assertNotIn("changeDeck", self.fake.calls)
        self.assertNotIn("createDeck", self.fake.calls)

    def test_zero_matches_makes_no_proposal(self):
        self.search_mock.return_value = ([], {}, {})
        s = self.session()
        out = s._dispatch("propose_move", {"selection": "nothing", "target_deck": "GI"})
        self.assertEqual(out["notes_matched"], 0)
        self.assertEqual(s.proposals, [])

    def test_missing_destination_and_anki_down_are_reported_to_the_model(self):
        s = self.session()
        self.assertEqual(s._dispatch("propose_move", {"selection": "x", "target_deck": ""})["error"],
                         "missing_target_deck")
        with patch.object(anki_connect, "invoke", side_effect=anki_connect.AnkiConnectError(anki_connect.NOT_RUNNING)):
            out = s._dispatch("propose_move", {"selection": "x", "target_deck": "GI"})
        self.assertEqual(out["error"], "anki_unavailable")
        self.assertEqual(s.proposals, [])

    def test_first_round_must_use_a_tool_and_reply_is_constrained(self):
        s = self.session(require_deck_search=True)
        self.assertEqual(s._tool_choice(0), "required")
        self.assertEqual(s._tool_choice(1), "auto")
        plain = self.llm.AgentSession("q", require_deck_search=True)   # no move tool
        self.assertEqual(plain._tool_choice(0)["function"]["name"], "search_deck")
        out = s._dispatch("propose_move", {"selection": "B&B GI", "target_deck": "GI"})
        self.assertIn("Review & move", out["reply_instructions"])
        self.assertIn("Do not call more tools", out["reply_instructions"])

    def test_rate_limit_waits_and_resends_the_same_request(self):
        class RateLimitError(Exception):
            status_code = 429
        calls = []
        def create(**kw):
            calls.append(kw)
            if len(calls) < 3:
                raise RateLimitError("Rate limit reached... Please try again in 1.1025s.")
            return "ok"
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(self.llm.time, "sleep") as sleep:
            self.assertEqual(self.llm._create(client, model="m"), "ok")
        self.assertEqual(len(calls), 3)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], 1.6025)
        def other(**kw):
            raise TimeoutError("not a rate limit")
        client.chat.completions.create = other
        with self.assertRaises(TimeoutError):
            self.llm._create(client, model="m")

    def test_plain_agent_without_the_tool_cannot_propose(self):
        s = self.llm.AgentSession("q")                       # default AGENT_TOOLS
        self.assertEqual(s._dispatch("propose_move", {"selection": "x", "target_deck": "GI"})["error"],
                         "unknown_tool:propose_move")

    def test_chat_route_returns_proposals_outside_debug(self):
        proposal = {"id": 1, "selection": "B&B GI", "target": "GI", "target_exists": True,
                    "suggestions": [], "count": 2, "nids": [1, 2]}
        client = app_module.app.test_client()
        msg = {"messages": [{"role": "user", "content": "move B&B GI to GI"}]}
        with patch.object(self.llm, "agent_chat",
                          return_value=("2 notes matched.", [], {"move_proposals": [proposal]})):
            body = client.post("/api/agent", json=msg).get_json()
        self.assertEqual(body["proposals"], [proposal])
        self.assertNotIn("move_proposals", body["debug"])
        # The Tutor route never returns proposals, even if one leaked into debug.
        with patch.object(self.llm, "tutor_chat",
                          return_value=("Answer.", [], {"move_proposals": [proposal]})):
            body = client.post("/api/tutor", json=msg).get_json()
        self.assertEqual(set(body), {"answer", "sources", "debug"})
        self.assertNotIn("move_proposals", body["debug"])

    def test_notes_endpoint_returns_requested_notes_and_missing_ids(self):
        notes = {7: {"guid": "g7", "nid": 7, "front": "Achalasia", "back": "", "front_html": "<img src='a.png'>",
                     "back_html": "", "extras_html": [], "tags": [], "deck": "GI"}}
        with patch.object(app_module.search, "notes_by_nids", return_value=notes):
            body = app_module.app.test_client().post("/api/notes", json={"nids": [7, 8]}).get_json()
        self.assertEqual([n["nid"] for n in body["notes"]], [7])
        self.assertIn("/media?filename=a.png", body["notes"][0]["front_html"])
        self.assertEqual(body["missing"], [8])
        too_many = app_module.app.test_client().post("/api/notes", json={"nids": list(range(101))})
        self.assertEqual(too_many.status_code, 400)


if __name__ == "__main__":
    unittest.main()
