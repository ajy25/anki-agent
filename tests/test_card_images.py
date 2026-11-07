"""Card image regression tests; no live collection or LLM calls required.

Run with: python -m unittest discover -s tests -v
"""

import base64
import csv
import importlib
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from html.parser import HTMLParser

from ankiagent.media import resolve_media_dir
from ankiagent.parse import html_to_text, iter_notes_anki2, render_html


# Import the app without rebuilding or querying the user's real collection.
with patch.dict(os.environ, {"DECK_PATH": "/unused/collection.anki2", "DECK_NAME": ""}):
    from ankiagent import ingest
with patch.object(ingest, "ensure_built", return_value=None):
    app_module = importlib.import_module("ankiagent.app")


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aC1sAAAAASUVORK5CYII="
)


class Images(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.images = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


class CardImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / "collection.media"
        self.media.mkdir()
        self.config = patch.dict(app_module.app.config, {"TESTING": True, "MEDIA_DIR": self.media})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.client = app_module.app.test_client()

    def test_media_directory_default_and_override(self):
        with patch.dict(os.environ, {"MEDIA_DIR": ""}):
            self.assertEqual(resolve_media_dir(self.root / "collection.anki2"), self.media.resolve())
        with patch.dict(os.environ, {"MEDIA_DIR": str(self.root / "custom")}):
            self.assertEqual(resolve_media_dir(self.root / "export.txt"), (self.root / "custom").resolve())

    def test_images_preserved_in_clozes_but_not_search_text(self):
        raw = '{{c1::<img src="diagram.png">::hint}}<b>heart</b>[sound:a.mp3]<audio>x</audio>'
        rendered = render_html(raw)
        self.assertIn('<mark data-cloze="1"><img src="diagram.png"></mark>', rendered)
        self.assertNotIn("audio", rendered)
        self.assertNotIn("[sound:", rendered)
        self.assertEqual(html_to_text('<img src="diagram.png"><b>heart</b>'), "heart")

    def test_local_filenames_survive_presentation_and_file_delivery(self):
        for filename, source in [
            ("diagram.png", "diagram.png"),
            ("heart image.png", "heart%20image.png"),
            ("résumé & #1.png", "résumé & #1.png"),
            ("100%.png", "100%25.png"),
            ("a%20b.png", "a%2520b.png"),
        ]:
            with self.subTest(source=source):
                (self.media / filename).write_bytes(PNG)
                markup = app_module._sanitize(f'<img src="{source}" width="900" onerror="alert(1)">')
                image = Images(markup).images[0]
                self.assertNotIn("onerror", image)
                self.assertEqual(image["loading"], "lazy")
                self.assertEqual(parse_qs(urlsplit(image["src"]).query)["filename"], [filename])
                response = self.client.get(image["src"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, PNG)
                self.assertEqual(response.mimetype, "image/png")
                response.close()

    def test_remote_images_and_unsafe_sources(self):
        for src in ("https://example.com/image.png", "http://example.com/image.png", "//example.com/image.png"):
            image = Images(app_module._sanitize(f'<img src="{src}">')).images[0]
            self.assertEqual(image["src"], "https:" + src if src.startswith("//") else src)
        for src in ("javascript:alert(1)", "file:///etc/passwd", "../secret.png", "%2e%2e%2fsecret.png", "/etc/secret.png", "data:text/html,hi"):
            with self.subTest(src=src):
                image = Images(app_module._sanitize(f'<img src="{src}">')).images[0]
                self.assertNotIn("src", image)
        markup = app_module._sanitize('<img src="a.png" srcset="evil.png 2x" style="position:fixed" onclick="alert(1)">')
        self.assertEqual(set(Images(markup).images[0]), {"src", "loading", "decoding"})

    def test_missing_files_traversal_non_images_and_symlinks_are_blocked(self):
        (self.root / "outside.png").write_bytes(PNG)
        (self.media / "escape.png").symlink_to(self.root / "outside.png")
        (self.media / "private.txt").write_text("private")
        for name in ("missing.png", "../outside.png", "..\\outside.png", "/outside.png", "escape.png", "private.txt", "", "\x00.png"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get("/media", query_string={"filename": name}).status_code, 404)

    def test_svg_is_served_with_sandbox_and_correct_type(self):
        (self.media / "occlusion.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        response = self.client.get("/media?filename=occlusion.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/svg+xml")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response.close()

    def test_tsv_import_cache_upgrade_search_note_and_tutor_sources(self):
        deck = self.root / "export.txt"
        db = self.root / "index.db"
        raw = '<img src="diagram.png">'
        with deck.open("w", newline="") as f:
            csv.writer(f, delimiter="\t").writerow(
                ["heart" + raw, raw, raw] + [""] * 14 + ["test-guid", "test-tag"]
            )
        ingest.build(deck_path=deck, db_path=db, deck_name=None)
        self.assertFalse(ingest.needs_rebuild(db, deck, deck_name=None))
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        self.assertTrue(ingest.needs_rebuild(db, deck, deck_name=None))
        with closing(sqlite3.connect(db)) as con, patch.object(app_module.search, "get_conn", return_value=con):
            con.row_factory = sqlite3.Row
            note = self.client.get("/api/note/test-guid").get_json()
            results = self.client.get("/api/search?q=heart").get_json()["results"]
            self.assertEqual(len(results), 1)
            for result in (note, results[0]):
                for value in (result["front_html"], result["back_html"], *result["extras_html"]):
                    self.assertEqual(Images(value).images[0]["src"], "/media?filename=diagram.png")
            # The tutor's result still goes through the same presentation path.
            raw_source = {
                **note, "front_html": "heart" + raw, "back_html": raw,
                "extras_html": [raw], "ref": 1, "cited": True,
            }
            fake_llm = SimpleNamespace(
                tutor_chat=Mock(return_value=("Heart [1]", [raw_source], {}))
            )
            with patch.dict("sys.modules", {"ankiagent.llm": fake_llm}):
                response = self.client.post("/api/tutor", json={
                    "messages": [{"role": "user", "content": "heart"}]
                })
                self.assertEqual(response.status_code, 200)
                source = response.get_json()["sources"][0]
            self.assertEqual(Images(source["front_html"]).images[0]["src"], "/media?filename=diagram.png")
            self.assertEqual(source["ref"], 1)

    def test_live_collection_import_preserves_images_in_all_fields(self):
        deck = self.root / "collection.anki2"
        with closing(sqlite3.connect(deck)) as con, con:
            con.executescript("""
                CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT);
                INSERT INTO fields VALUES (1, 0, 'Front'), (1, 1, 'Back'), (1, 2, 'More');
                CREATE TABLE decks (id INTEGER, name TEXT);
                INSERT INTO decks VALUES (1, 'Test');
                CREATE TABLE cards (nid INTEGER, did INTEGER);
                INSERT INTO cards VALUES (1, 1);
                CREATE TABLE notes (id INTEGER, guid TEXT, flds TEXT, tags TEXT, mid INTEGER);
            """)
            raw = '<img src="diagram.png">'
            con.execute("INSERT INTO notes VALUES (1, 'guid', ?, '', 1)", ("\x1f".join([raw] * 3),))
        note, = iter_notes_anki2(deck)
        for value in (note.front_html, note.back_html, *note.extras_html):
            self.assertEqual(Images(value).images[0]["src"], "diagram.png")


if __name__ == "__main__":
    unittest.main()
