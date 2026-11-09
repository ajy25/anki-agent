"""browse_tags lets the AI walk the tag tree when it can't guess a name
(e.g. "Boards and Beyond" is tagged "#B&B")."""

import os
import sqlite3
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest, search


class TagBrowseTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(ingest.SCHEMA)
        self.con.executemany("INSERT INTO note_tags VALUES (?, ?)", [
            ("a", "#AK_Step1_v12::#B&B::10_GI::01_Esophagus"),
            ("a", "#AK_Step1_v12::#B&B::10_GI::02_Stomach"),      # same note, counted once
            ("b", "#AK_Step1_v12::#B&B::10_GI::02_Stomach"),
            ("c", "#AK_Step1_v12::#B&B::21_Renal"),
            ("d", "#AK_Step1_v12::#Pathoma::Ch10"),
            ("e", "#AK_Other::Boards_and_Beyond_Micro"),
            ("f", "#AK_Step1_v12"),                               # no child level
        ])
        p = patch.object(search, "get_conn", return_value=self.con)
        p.start()
        self.addCleanup(p.stop)

    def test_top_level(self):
        self.assertEqual(search.tag_children(""), [
            {"tag": "#AK_Step1_v12", "notes": 5},
            {"tag": "#AK_Other", "notes": 1},
        ])

    def test_descends_to_resource_and_system_with_distinct_note_counts(self):
        self.assertEqual(search.tag_children("#AK_Step1_v12"), [
            {"tag": "#AK_Step1_v12::#B&B", "notes": 3},
            {"tag": "#AK_Step1_v12::#Pathoma", "notes": 1},
        ])
        self.assertEqual(search.tag_children("#ak_step1_v12::#b&b::"), [
            {"tag": "#AK_Step1_v12::#B&B::10_GI", "notes": 2},
            {"tag": "#AK_Step1_v12::#B&B::21_Renal", "notes": 1},
        ])

    def test_like_wildcards_in_prefix_are_literal(self):
        self.assertEqual(search.tag_children("#AK%"), [])


if __name__ == "__main__":
    unittest.main()
