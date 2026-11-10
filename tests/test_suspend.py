"""Suspend / unsuspend from Anki Agent: preview, apply, undo, and the agent tool.

Uses an in-memory fake AnkiConnect that understands the small slice of Anki
search syntax the app sends ("deck:X", nid:, -nid:, cid:, is:suspended).
"""

import importlib
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import anki_connect, ingest, organize
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")
from ankiagent import llm

GI = "Study Deck::GI subdeck"


class FakeAnki:
    def __init__(self):
        # card id -> [note id, deck, suspended]
        self.cards = {
            101: [1, GI, False], 102: [1, GI, False],    # note 1: GERD (2 cloze cards)
            201: [2, GI, False],                          # note 2: achalasia
            301: [3, GI, True],                           # note 3: already suspended
            401: [4, "Study Deck", False],          # note 4: GERD, other deck
        }
        self.calls = []
        self.media_path = str(ingest.DECK_PATH.parent / "collection.media")

    def _match(self, cid, query):
        nid, deck, susp = self.cards[cid]
        terms = re.findall(r'(-?)(?:"(deck|nid|cid|is):((?:[^"\\]|\\.)*)"|(deck|nid|cid|is):(\S+))', query)
        for neg, qkey, qval, key, val in terms:
            key, val = (qkey, qval) if qkey else (key, val)
            val = val.replace("\\_", "_").replace("\\*", "*")
            if key == "deck":
                ok = deck == val or deck.startswith(val + "::")
            elif key in ("nid", "cid"):
                ok = (nid if key == "nid" else cid) in {int(x) for x in val.split(",")}
            else:
                ok = susp
            if ok == bool(neg):
                return False
        return True

    def invoke(self, action, **p):
        self.calls.append(action)
        if action == "version":
            return 6
        if action == "getMediaDirPath":
            return self.media_path
        if action == "deckNames":
            return sorted({c[1] for c in self.cards.values()})
        if action == "findCards":
            return sorted(c for c in self.cards if self._match(c, p["query"]))
        if action == "findNotes":
            return sorted({self.cards[c][0] for c in self.cards if self._match(c, p["query"])})
        if action in ("suspend", "unsuspend"):
            for c in p["cards"]:
                self.cards[c][2] = action == "suspend"
            return True
        raise AssertionError(action)

    def suspended(self):
        return {c for c, v in self.cards.items() if v[2]}


class Base(unittest.TestCase):
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


class SuspendTests(Base):
    def test_spec_requires_a_scope_and_a_known_action(self):
        with self.assertRaises(ValueError):
            organize.clean_card_spec({"action": "suspend"})            # whole collection
        with self.assertRaises(ValueError):
            organize.clean_card_spec({"action": "delete", "deck": GI})
        spec = organize.clean_card_spec({"action": "suspend", "deck": GI, "exclude_nids": [1]})
        self.assertEqual(spec, {"action": "suspend", "deck": GI, "nids": None, "exclude_nids": [1]})

    def test_suspend_all_except_then_undo(self):
        spec = organize.clean_card_spec({"action": "suspend", "deck": GI, "exclude_nids": [1]})
        p = organize.preview_card_action(spec)
        self.assertEqual((p["cards_to_change"], p["already"], p["kept"]), (1, 1, 2))
        self.assertEqual((p["change_nids"], p["kept_nids"]), ([2], [1]))   # for the card lists
        self.assertNotIn("suspend", self.fake.calls)

        r = organize.apply_card_action(spec, preview_id=p["preview_id"], label="suspend all but GERD")
        self.assertEqual(r["changed"], 1)
        self.assertEqual(self.fake.suspended(), {201, 301})         # GERD (101,102) kept
        self.assertEqual(organize.list_moves()[0]["action"], "suspend")

        u = organize.undo_move(r["id"])
        self.assertEqual(u["restored"], 1)
        self.assertEqual(self.fake.suspended(), {301})              # pre-existing stays
        with self.assertRaises(ValueError):
            organize.undo_move(r["id"])

    def test_unknown_deck_is_refused(self):
        spec = organize.clean_card_spec({"action": "suspend", "deck": "Nope"})
        with self.assertRaises(ValueError):
            organize.preview_card_action(spec)

    def test_routes_are_guarded_and_work(self):
        client = app_module.app.test_client()
        body = {"action": "suspend", "deck": GI, "exclude_nids": [1]}
        self.assertEqual(client.post("/api/cards/apply", json=body,
                         headers={"Origin": "https://evil.example"}).status_code, 403)
        preview = client.post("/api/cards/preview", json=body).get_json()
        self.assertEqual(preview["cards_to_change"], 1)
        body["preview_id"] = preview["preview_id"]
        r = client.post("/api/cards/apply", json=body).get_json()
        self.assertEqual(r["changed"], 1)
        self.assertEqual(client.post(f"/api/moves/{r['id']}/undo", json={}).get_json()["restored"], 1)


class AgentSuspendToolTests(Base):
    def setUp(self):
        super().setUp()
        self.found = {"GERD": [{"nid": 1, "front": "GERD: LES relaxation", "tags": []},
                               {"nid": 4, "front": "GERD complications", "tags": []}]}
        p = patch.object(llm, "assisted_search",
                         side_effect=lambda q, **kw: (self.found.get(q, []), {}, {}))
        p.start()
        self.addCleanup(p.stop)
        direct = patch.object(llm, "direct_tag_selection", return_value=None)
        direct.start()
        self.addCleanup(direct.stop)

    def session(self, q="within the GI subdeck suspend all the cards except for those about GERD"):
        return llm.AgentSession(q, tools=llm.AGENT_ORGANIZER_TOOLS, require_deck_search=True)

    def test_prepares_suspend_all_except_without_changing_anything(self):
        s = self.session()
        kw = patch.object(llm.search, "fast_search", return_value=[])
        kw.start()
        self.addCleanup(kw.stop)
        out = s._dispatch("propose_card_action",
                          {"action": "suspend", "deck": "GI subdeck", "except_selection": "GERD"})
        self.assertEqual(out["deck"], GI)                  # unique leaf -> full path
        self.assertEqual((out["cards_to_change"], out["kept_cards"]), (1, 2))
        self.assertEqual(out["kept_sample"], ["GERD: LES relaxation"])
        self.assertIn("Review & suspend", out["reply_instructions"])
        p = s.proposals[0]
        # Note 4 is about GERD but not in the GI subdeck, so it isn't part of the keep list.
        self.assertEqual((p["action"], p["nids"], p["exclude_nids"]), ("suspend", None, [1]))
        self.assertNotIn("suspend", self.fake.calls)

    def test_shortened_deck_name_resolves_from_the_message(self):
        decks = [GI, "Study Deck", "Study Deck::GI cards", "Default"]
        msg = "within the GI subdeck I want you to suspend all the cards except for those about GERD"
        self.assertEqual(organize.resolve_target("GI", decks, msg)["target"], GI)
        # Without the message, "GI" is ambiguous; both GI decks are suggested.
        r = organize.resolve_target("GI", decks)
        self.assertFalse(r["exists"])
        self.assertIn(GI, r["suggestions"])
        self.assertIn("Study Deck::GI cards", r["suggestions"])

    def test_keep_set_adds_keyword_matches_to_the_ai_search(self):
        self.found["GERD"] = []                         # AI tag search came back empty
        hits = [{"nid": 1, "front": "Heartburn is GERD", "tags": []}]
        with patch.object(llm.search, "fast_search", return_value=hits) as fs:
            out = self.session()._dispatch("propose_card_action",
                {"action": "suspend", "deck": "GI subdeck", "except_selection": "GERD"})
        self.assertEqual(fs.call_args.kwargs["any_keywords"], ["GERD"])
        self.assertEqual((out["cards_to_change"], out["kept_cards"]), (1, 2))

    def test_missing_exception_never_suspends_everything(self):
        s = self.session()
        with patch.object(llm.search, "fast_search", return_value=[]):
            out = s._dispatch("propose_card_action",
                              {"action": "suspend", "deck": "GI subdeck", "except_selection": "Zollinger"})
        self.assertEqual(out["error"], "exception_not_found")
        self.assertEqual(s.proposals, [])

    def test_empty_deck_is_reported_as_empty_not_already_suspended(self):
        for c in self.fake.cards.values():
            c[1] = "Study Deck"                     # GI subdeck emptied (e.g. move undone)
        with patch.object(llm.search, "fast_search", return_value=[]), \
             patch.object(organize.anki, "deck_names", return_value=[GI, "Study Deck"]):
            out = self.session()._dispatch("propose_card_action",
                {"action": "suspend", "deck": "GI subdeck", "except_selection": "GERD"})
        self.assertEqual(out["cards_to_change"], 0)
        self.assertIn("has no cards", out["note"])

    def test_keep_list_is_trimmed_to_the_deck_so_huge_matches_still_preview(self):
        # "except Boards and Beyond" can match more notes collection-wide than
        # MAX_NOTES; only the deck's own notes may reach the preview.
        self.found["Boards and Beyond"] = [{"nid": n, "front": f"card {n}", "tags": []}
                                           for n in [1] + list(range(1000, 1000 + organize.MAX_NOTES + 5))]
        s = self.session("in my GI subdeck suspend everything except Boards and Beyond")
        with patch.object(llm.search, "fast_search", return_value=[]):
            out = s._dispatch("propose_card_action",
                              {"action": "suspend", "deck": "GI subdeck", "except_selection": "Boards and Beyond"})
        self.assertNotIn("error", out)
        self.assertEqual(s.proposals[0]["exclude_nids"], [1])
        organize.clean_card_spec(s.proposals[0])          # what the preview checks: no cap error

    def test_keep_cards_outside_the_deck_never_mean_suspend_everything(self):
        self.found["Pathoma"] = [{"nid": 999, "front": "elsewhere", "tags": []}]
        s = self.session("in my GI subdeck suspend everything except Pathoma")
        with patch.object(llm.search, "fast_search", return_value=[]):
            out = s._dispatch("propose_card_action",
                              {"action": "suspend", "deck": "GI subdeck", "except_selection": "Pathoma"})
        self.assertEqual(out["error"], "exception_not_in_deck")
        self.assertEqual(s.proposals, [])

    def test_unknown_deck_and_bad_action(self):
        s = self.session()
        self.assertEqual(s._dispatch("propose_card_action", {"action": "suspend", "deck": "Cardio"})["error"],
                         "deck_not_found")
        self.assertIn("error", s._dispatch("propose_card_action", {"action": "delete", "deck": "GI subdeck"}))
        self.assertEqual(s._dispatch("propose_card_action", {"action": "suspend"})["error"], "missing_scope")

    def test_text_reply_to_required_tool_round_falls_back_instead_of_failing(self):
        calls = []
        def create(**kw):
            calls.append(kw["tool_choice"])
            if kw["tool_choice"] == "required":
                raise RuntimeError("Tool choice is required, but model did not call a tool")
            msg = SimpleNamespace(content="Hi! Ask me about your cards.", reasoning="", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm, "get_client", return_value=client):
            answer, _, debug = self.session("hi").run()
        self.assertEqual(answer, "Hi! Ask me about your cards.")
        self.assertEqual(calls, ["required", "auto"])
        self.assertTrue(debug["tool_choice_fallback"])
        self.assertNotIn("error", debug)


if __name__ == "__main__":
    unittest.main()
