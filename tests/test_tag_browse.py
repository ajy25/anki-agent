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
            ("a", "#Study_Step1::#B&B::10_GI::01_Esophagus"),
            ("a", "#Study_Step1::#B&B::10_GI::02_Stomach"),      # same note, counted once
            ("b", "#Study_Step1::#B&B::10_GI::02_Stomach"),
            ("c", "#Study_Step1::#B&B::21_Renal"),
            ("d", "#Study_Step1::#Pathoma::Ch10"),
            ("e", "#Study_Other::Boards_and_Beyond_Micro"),
            ("f", "#Study_Step1"),                               # no child level
        ])
        p = patch.object(search, "get_conn", return_value=self.con)
        p.start()
        self.addCleanup(p.stop)

    def test_top_level(self):
        self.assertEqual(search.tag_children(""), [
            {"tag": "#Study_Step1", "notes": 5},
            {"tag": "#Study_Other", "notes": 1},
        ])

    def test_descends_to_resource_and_system_with_distinct_note_counts(self):
        self.assertEqual(search.tag_children("#Study_Step1"), [
            {"tag": "#Study_Step1::#B&B", "notes": 3},
            {"tag": "#Study_Step1::#Pathoma", "notes": 1},
        ])
        self.assertEqual(search.tag_children("#study_step1::#b&b::"), [
            {"tag": "#Study_Step1::#B&B::10_GI", "notes": 2},
            {"tag": "#Study_Step1::#B&B::21_Renal", "notes": 1},
        ])

    def test_like_wildcards_in_prefix_are_literal(self):
        self.assertEqual(search.tag_children("#AK%"), [])


if __name__ == "__main__":
    unittest.main()
