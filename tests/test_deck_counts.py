"""Deck totals count notes once even when their cards span subdecks."""

import importlib
import os
import sqlite3
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest, search
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")


class DeckCountTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(ingest.SCHEMA)
        notes = ("shared", "parent", "a", "b", "nested", "other")
        self.con.executemany(
            "INSERT INTO notes (guid, front, back, front_html, back_html, extras, tags_raw) "
            "VALUES (?, 'front', '', '', '', '[]', '')", [(guid,) for guid in notes],
        )
        self.con.executemany("INSERT INTO note_decks VALUES (?, ?)", [
            ("shared", "Root"), ("parent", "Root"),
            ("shared", "Root::A"), ("a", "Root::A"),
            ("shared", "Root::B"), ("b", "Root::B"),
            ("shared", "Root::A::Nested"), ("nested", "Root::A::Nested"),
            ("shared", "Elsewhere"), ("other", "Elsewhere"),
        ])
        # Existing caches store exact-deck counts. No cache rebuild should be
        # necessary to correct their inflated parent totals.
        self.con.execute(
            "INSERT INTO decks SELECT deck, COUNT(*) FROM note_decks GROUP BY deck"
        )
        self.con.execute("INSERT INTO meta VALUES ('note_count', '6')")
        conn_patch = patch.object(search, "get_conn", return_value=self.con)
        conn_patch.start()
        self.addCleanup(conn_patch.stop)
        self.client = app_module.app.test_client()

    def test_parent_counts_deduplicate_direct_and_nested_memberships(self):
        roots = {node["path"]: node for node in self.client.get("/api/decks").get_json()}
        self.assertEqual(roots["Root"]["count"], 5)
        children = {node["path"]: node for node in roots["Root"]["children"]}
        self.assertEqual(children["Root::A"]["count"], 3)
        self.assertEqual(children["Root::A"]["children"][0]["count"], 2)
        self.assertEqual(children["Root::B"]["count"], 2)
        self.assertEqual(roots["Elsewhere"]["count"], 2)
        self.assertEqual(self.client.get("/api/stats").get_json()["notes"], 6)

    def test_every_deck_count_matches_its_search_results(self):
        pending = search.deck_tree()
        while pending:
            node = pending.pop()
            with self.subTest(deck=node["path"]):
                results = self.client.get("/api/search", query_string={
                    "deck": node["path"], "limit": 100,
                }).get_json()["results"]
                self.assertEqual(node["count"], len(results))
            pending.extend(node["children"])

    def test_sources_without_deck_memberships_have_no_sidebar_tree(self):
        self.con.execute("DELETE FROM note_decks")
        self.assertEqual(search.deck_tree(), [])


if __name__ == "__main__":
    unittest.main()
