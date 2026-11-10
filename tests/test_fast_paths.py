"""Faster card moves: the direct tag shortcut, and failing fast on rate limits.

Uses an in-memory search index and fake LLM clients; nothing touches Anki
or the network.
"""

import importlib
import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import anki_connect, ingest, llm, search
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")

S1 = "#AK_Step1_v12::#B&B"
S2 = "#AK_Step2_v12::#B&B"
NOTES = {  # guid -> (nid, deck, tags)
    "a": (1, "AnKing", [f"{S1}::10_GI::01_Anatomy::01_Gastrointestinal_Embryology"]),
    "b": (2, "AnKing", [f"{S1}::10_GI::01_Anatomy::02_Gastrointestinal_Anatomy::Extra"]),
    "c": (3, "Other", [f"{S1}::10_GI::01_Anatomy"]),
    "d": (4, "AnKing", [f"{S1}::10_GIX"]),                    # shares a prefix, not beneath
    "e": (5, "AnKing", [f"{S1}::10_GI::02_GI_Physiology"]),
    "f": (6, "AnKing", [f"{S2}::06_GI::01_Esophagus"]),
    "g": (7, "AnKing", [f"{S1}::06_Cardio::01_Anatomy"]),
    "h": (8, "AnKing", ["#AK_Step1_v12::#Pathoma::Ch10"]),
}


class IndexBase(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(ingest.SCHEMA)
        counts = {}
        for guid, (nid, deck, tags) in NOTES.items():
            self.con.execute(
                "INSERT INTO notes (guid, front, back, front_html, back_html, extras, tags_raw, deck, nid)"
                " VALUES (?, ?, '', '', '', '[]', ?, ?, ?)", (guid, f"card {guid}", " ".join(tags), deck, nid))
            self.con.execute("INSERT INTO note_decks VALUES (?, ?)", (guid, deck))
            for t in tags:
                self.con.execute("INSERT INTO note_tags VALUES (?, ?)", (guid, t))
                counts[t] = counts.get(t, 0) + 1
        self.con.executemany("INSERT INTO tags VALUES (?, ?)", counts.items())
        p = patch.object(search, "get_conn", return_value=self.con)
        p.start()
        self.addCleanup(p.stop)

    def select(self, text, deck=""):
        out = llm.direct_tag_selection(text, deck=deck)
        return None if out is None else (sorted(n["nid"] for n in out[0]), out[1])


class DirectTagSelectionTests(IndexBase):
    def test_resource_system_and_topic_pick_one_subtree(self):
        for text in ("all the cards for the GI anatomy boards and beyond videos",
                     "Boards and Beyond GI anatomy cards", "B&B gastrointestinal anatomy"):
            self.assertEqual(self.select(text), ([1, 2, 3], [f"{S1}::10_GI::01_Anatomy"]), text)

    def test_same_subtree_in_two_decks_is_combined(self):
        self.assertEqual(self.select("Boards and Beyond GI"),
                         ([1, 2, 3, 5, 6], [f"{S1}::10_GI", f"{S2}::06_GI"]))

    def test_subtree_matches_whole_levels_only(self):
        nids = [n["nid"] for n in search.notes_under_tags([f"{S1}::10_GI"])]
        self.assertEqual(nids, [1, 2, 3, 5])                    # not "10_GIX"

    def test_deck_scope_is_applied(self):
        self.assertEqual(self.select("B&B GI anatomy", deck="AnKing")[0], [1, 2])

    def test_no_resource_or_unmatched_topic_falls_back_to_ai(self):
        self.assertIsNone(self.select("GI anatomy"))                       # no resource named
        self.assertIsNone(self.select("Boards and Beyond GERD cards"))     # no GERD tag here
        self.assertIsNone(self.select("Boards and Beyond and Pathoma GI"))  # two resources

    def test_ambiguous_match_falls_back_to_ai(self):
        # "anatomy" alone matches GI and Cardio subtrees in the same deck.
        self.assertIsNone(self.select("Boards and Beyond anatomy"))
        self.con.execute("INSERT INTO tags VALUES (?, 1)", (f"{S1}::10_GI::01_Anatomy_Extra",))
        self.assertIsNone(self.select("Boards and Beyond GI anatomy"))

    def test_deck_context_breaks_a_tie_toward_the_dominant_subtree(self):
        # Notes 1-3 are GI anatomy; note 3 also carries a Cardio anatomy tag.
        self.con.execute("UPDATE note_decks SET deck = 'GI' WHERE guid IN ('a', 'b', 'c')")
        self.con.execute("INSERT INTO note_tags VALUES ('a', ?)", (f"{S1}::06_Cardio::01_Anatomy",))
        self.assertIsNone(self.select("Boards and Beyond anatomy"))          # no deck: ambiguous
        out = llm.direct_tag_selection("Boards and Beyond anatomy section", context_deck="GI")
        self.assertEqual(out[1], [f"{S1}::10_GI::01_Anatomy"])
        self.assertEqual(sorted(n["nid"] for n in out[0]), [1, 2, 3])

    def test_no_dominant_subtree_stays_ambiguous(self):
        self.con.execute("UPDATE note_decks SET deck = 'Mixed' WHERE guid IN ('a', 'g')")
        self.assertIsNone(llm.direct_tag_selection("Boards and Beyond anatomy", context_deck="Mixed"))

    def test_short_topic_rule_for_the_keyword_add_on(self):
        self.assertTrue(llm._is_short_topic("GERD"))
        self.assertTrue(llm._is_short_topic("liver disease"))
        self.assertFalse(llm._is_short_topic("anatomy section for boards and beyond"))
        self.assertFalse(llm._is_short_topic("cards about portal hypertension and varices bleeding"))

    def test_propose_move_uses_the_tag_without_model_calls(self):
        fake = SimpleNamespace(calls=[])

        def invoke(action, **params):
            fake.calls.append(action)
            return {"version": 6, "deckNames": ["Default", "AnKing::GI"]}[action]

        events = []
        with patch.object(anki_connect, "invoke", side_effect=invoke), \
                patch.object(llm, "assisted_search", side_effect=AssertionError("no model")):
            s = llm.AgentSession("move B&B GI anatomy to GI", tools=llm.AGENT_ORGANIZER_TOOLS)
            s._on_event = events.append
            out = s._dispatch("propose_move", {"selection": "Boards and Beyond GI anatomy",
                                               "target_deck": "GI"})
        self.assertEqual(out["notes_matched"], 3)
        self.assertEqual(out["target_deck"], "AnKing::GI")
        self.assertEqual(s.proposals[0]["nids"], [1, 2, 3])
        self.assertIn("Matched tag", events[0]["step"]["text"])
        self.assertNotIn("changeDeck", fake.calls)


class DeckNameTests(unittest.TestCase):
    def test_possessive_names_resolve_to_the_existing_deck(self):
        from ankiagent import organize
        decks = ["Default", "AnKing Step Deck::GI subdeck", "AnKing Step Deck::GI cards"]
        msg = "move the GI anatomy boards and beyond cards to my gi subdeck"
        self.assertEqual(organize.resolve_target("my gi subdeck", decks, msg)["target"],
                         "AnKing Step Deck::GI subdeck")
        self.assertEqual(organize.resolve_target("the GI cards", decks, "")["target"],
                         "AnKing Step Deck::GI cards")
        new = organize.resolve_target("My Deck", decks, "")       # a real new name stays as typed
        self.assertEqual((new["target"], new["exists"]), ("My Deck", False))


class FakeRateLimit(Exception):
    status_code = 429


class FakeServerError(Exception):
    status_code = 503


class FakeClient:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(llm, "_sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def test_daily_limit_fails_at_once_with_a_clear_message(self):
        client = FakeClient([FakeRateLimit(
            "Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): "
            "Limit 200000, Used 199660, Requested 1326. Please try again in 7m5.952s.")])
        with self.assertRaises(llm.RateLimitExhausted) as ctx:
            llm._create(client, model="openai/gpt-oss-120b")
        self.assertEqual(client.calls, 1)
        self.sleep.assert_not_called()
        self.assertIn("daily token limit", str(ctx.exception))
        self.assertIn("7 minutes", str(ctx.exception))

    def test_daily_limit_with_a_short_wait_is_retried(self):
        client = FakeClient([FakeRateLimit("tokens per day (TPD). Please try again in 18.2s.")])
        self.assertEqual(llm._create(client, model="m"), "ok")
        self.assertEqual(client.calls, 2)
        client = FakeClient([FakeRateLimit("tokens per day (TPD).")])     # no wait stated
        with self.assertRaises(llm.RateLimitExhausted):
            llm._create(client, model="m")

    def test_long_wait_fails_at_once(self):
        client = FakeClient([FakeRateLimit("tokens per minute (TPM). Please try again in 1m30s.")])
        with self.assertRaises(llm.RateLimitExhausted):
            llm._create(client, model="m")
        self.assertEqual(client.calls, 1)

    def test_short_wait_is_shown_then_retried(self):
        client = FakeClient([FakeRateLimit("tokens per minute (TPM). Please try again in 2.5s.")])
        waits = []
        self.assertEqual(llm._create(client, model="m", on_wait=waits.append), "ok")
        self.assertEqual(waits, [3.0])
        self.assertEqual(client.calls, 2)

    def test_server_error_is_retried_once(self):
        client = FakeClient([FakeServerError("503")])
        self.assertEqual(llm._create(client, model="m"), "ok")
        client = FakeClient([FakeServerError("503"), FakeServerError("503")])
        with self.assertRaises(FakeServerError):
            llm._create(client, model="m")

    def test_retry_after_parsing(self):
        self.assertEqual(llm._retry_after(Exception("try again in 7m5.5s")), 425.5)
        self.assertEqual(llm._retry_after(Exception("try again in 1h2m")), 3720.0)
        self.assertAlmostEqual(llm._retry_after(Exception("try again in 530ms")), 0.53)
        self.assertIsNone(llm._retry_after(Exception("slow down")))

    def test_card_finder_rate_limit_is_not_reported_as_no_matches(self):
        with patch.object(llm, "direct_tag_selection", return_value=None), \
                patch.object(llm, "assisted_search", return_value=([], {}, {"rate_limited": "limit hit"})):
            s = llm.AgentSession("q", tools=llm.AGENT_ORGANIZER_TOOLS)
            with self.assertRaises(llm.RateLimitExhausted):
                s._select_cards("Boards and Beyond GERD", deck="")

    def test_chat_stops_without_retrying_and_reports_the_message(self):
        runs = []

        def run(self, **kw):
            runs.append(1)
            raise llm.RateLimitExhausted("Groq's daily token limit for m is used up.")

        events = []
        with patch.object(llm.AgentSession, "run", run):
            answer, sources, debug = llm.agent_chat(
                [{"role": "user", "content": "move B&B GI"}], on_event=events.append)
        self.assertEqual((answer, sources, len(runs)), ("", [], 1))
        self.assertEqual(debug["user_error"], "Groq's daily token limit for m is used up.")
        self.assertEqual(events[-1]["step"]["type"], "error")

    def test_endpoint_returns_the_rate_limit_message(self):
        debug = {"error": "rate_limited", "user_error": "Groq's daily token limit is used up."}
        with patch.object(llm, "agent_chat", return_value=("", [], debug)):
            res = app_module.app.test_client().post(
                "/api/agent", json={"messages": [{"role": "user", "content": "move"}]})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], "Groq's daily token limit is used up.")


if __name__ == "__main__":
    unittest.main()


class ConnectionPerThreadTests(unittest.TestCase):
    """A shared connection across server threads crashed SQLite; each thread
    must get its own."""

    def test_threads_get_separate_connections_and_reset_reopens(self):
        import tempfile
        import threading
        path = os.path.join(tempfile.mkdtemp(), "idx.db")
        sqlite3.connect(path).close()
        seen = []
        t = threading.Thread(target=lambda: seen.append(search.get_conn(path)))
        t.start()
        t.join()
        mine = search.get_conn(path)
        self.assertIsNot(seen[0], mine)
        self.assertIs(search.get_conn(path), mine)        # reused within a thread
        search.reset_conn()
        self.assertIsNot(search.get_conn(path), mine)     # reopened after a reset
        search.reset_conn()


class UndoFromChatTests(unittest.TestCase):
    """Anki Agent can prepare an undo of earlier changes; it never undoes itself."""

    HISTORY = [
        {"id": "m1", "created": 100, "action": "move", "target": "Deck::GI subdeck",
         "label": "Boards and Beyond GI cards", "status": "done", "cards": 3, "from_decks": ["Deck"]},
        {"id": "s1", "created": 200, "action": "suspend", "target": "Deck::GI subdeck",
         "label": "suspend non high yield", "status": "done", "cards": 2, "from_decks": []},
        {"id": "old", "created": 50, "action": "move", "target": "Deck::GI cards",
         "label": "", "status": "undone", "cards": 9, "from_decks": ["Deck"]},
    ]

    def setUp(self):
        from ankiagent import organize
        p = patch.object(organize, "list_moves",
                         side_effect=lambda limit=10: sorted(self.HISTORY, key=lambda m: -m["created"]))
        p.start()
        self.addCleanup(p.stop)
        undo = patch.object(organize, "undo_move", side_effect=AssertionError("must not undo"))
        undo.start()
        self.addCleanup(undo.stop)

    def session(self):
        return llm.AgentSession("undo that", tools=llm.AGENT_ORGANIZER_TOOLS)

    def test_prepares_undo_newest_first_without_changing_anything(self):
        s = self.session()
        out = s._dispatch("propose_undo", {"change_ids": ["m1", "s1"]})
        self.assertEqual(out["changes_to_undo"], 2)
        self.assertEqual([c["id"] for c in s.proposals[0]["changes"]], ["s1", "m1"])
        self.assertEqual((s.proposals[0]["action"], s.proposals[0]["count"]), ("undo", 5))

    def test_already_undone_and_unknown_ids(self):
        s = self.session()
        self.assertIn("already undone", s._dispatch("propose_undo", {"change_ids": ["old"]})["note"])
        self.assertEqual(s._dispatch("propose_undo", {"change_ids": ["nope"]})["error"], "unknown_change_ids")
        self.assertEqual(s._dispatch("propose_undo", {})["error"], "missing_change_ids")
        self.assertEqual(s.proposals, [])

    def test_tutor_cannot_undo(self):
        tutor = llm.AgentSession("q", tools=llm.TUTOR_TOOLS)
        self.assertEqual(tutor._dispatch("propose_undo", {"change_ids": ["m1"]})["error"],
                         "unknown_tool:propose_undo")

    def test_agent_prompt_lists_recent_changes_with_ids(self):
        text = llm._recent_changes_prompt()
        self.assertIn("id s1", text)
        self.assertIn("id old", text)
        self.assertIn("ALREADY UNDONE", text)
        self.assertLess(text.index("id s1"), text.index("id m1"))   # newest first

    def test_endpoint_passes_undo_changes_to_the_ui(self):
        proposal = {"id": 1, "action": "undo", "count": 3,
                    "changes": [{"id": "m1", "action": "move", "cards": 3, "target": "GI"}]}
        with patch.object(llm, "agent_chat",
                          return_value=("Ready to undo.", [], {"move_proposals": [proposal]})):
            res = app_module.app.test_client().post(
                "/api/agent", json={"messages": [{"role": "user", "content": "undo that"}]})
        self.assertEqual(res.get_json()["proposals"][0]["changes"][0]["id"], "m1")
