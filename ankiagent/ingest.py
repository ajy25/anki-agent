"""Build the SQLite + FTS5 index for the Anki deck.

Run directly: `python ingest.py [--force]`.

The DB is rebuilt only if the deck file mtime is newer than the cached DB,
unless --force is passed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .parse import iter_deck_notes


load_dotenv()

# Project root is the parent of this package; cache/ lives alongside the code,
# not inside the package directory.
ROOT = Path(__file__).resolve().parents[1]


def _resolve_deck_path() -> Path:
    """Deck source from DECK_PATH in .env (`~` and $VARS expanded).

    DECK_PATH may point at a live Anki collection (collection.anki2) or a TSV
    export. It is required — there is no bundled fallback.
    """
    raw = os.environ.get("DECK_PATH", "").strip()
    if not raw:
        raise RuntimeError(
            "DECK_PATH is not set. Add it to .env, e.g.\n"
            "  DECK_PATH=~/Library/Application Support/Anki2/<profile>/collection.anki2"
        )
    return Path(os.path.expandvars(os.path.expanduser(raw)))


DECK_PATH = _resolve_deck_path()
# Which deck to index from a live collection (Anki sidebar name, `::` between
# levels). Selects that deck and all subdecks. Empty = index the whole
# collection. Ignored for TSV exports.
DECK_NAME = os.environ.get("DECK_NAME", "").strip() or None
CACHE_DIR = ROOT / "cache"
DB_PATH = CACHE_DIR / "anki.db"

# Bump when the cache schema changes so existing caches rebuild automatically.
SCHEMA_VERSION = "3"


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    guid       TEXT PRIMARY KEY,
    front      TEXT NOT NULL,
    back       TEXT NOT NULL,
    front_html TEXT NOT NULL,
    back_html  TEXT NOT NULL,
    extras     TEXT NOT NULL,   -- JSON array of sanitized extra-field HTML
    tags_raw   TEXT NOT NULL,
    deck       TEXT NOT NULL DEFAULT '',  -- primary (first) deck path, for display
    nid        INTEGER NOT NULL DEFAULT 0 -- Anki note id (notes.id), searchable as nid:
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    guid UNINDEXED,
    front,
    back,
    tags,
    tokenize = "porter unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS tags (
    tag   TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS note_tags (
    guid TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (guid, tag)
);

CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag);

-- Deck membership: one row per (note, deck path) since a note can span decks.
CREATE TABLE IF NOT EXISTS note_decks (
    guid TEXT NOT NULL,
    deck TEXT NOT NULL,
    PRIMARY KEY (guid, deck)
);

CREATE INDEX IF NOT EXISTS idx_note_decks_deck ON note_decks(deck);

-- Per-deck note counts (exact path) for the sidebar deck tree.
CREATE TABLE IF NOT EXISTS decks (
    deck  TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


def needs_rebuild(
    db_path: Path, deck_path: Path, deck_name: str | None = DECK_NAME
) -> bool:
    if not db_path.exists():
        return True
    try:
        deck_mtime = deck_path.stat().st_mtime
    except FileNotFoundError:
        return False
    db_mtime = db_path.stat().st_mtime
    if deck_mtime > db_mtime:
        return True
    try:
        with sqlite3.connect(db_path) as con:
            (count,) = con.execute("SELECT COUNT(*) FROM notes").fetchone()
            if count == 0:
                return True
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return True
    # Rebuild if the cache predates the current schema.
    if meta.get("schema_version") != SCHEMA_VERSION:
        return True
    # Rebuild if the source path or deck filter changed since the cache was built.
    if meta.get("deck_path") != str(deck_path):
        return True
    if (meta.get("deck_name") or "") != (deck_name or ""):
        return True
    return False


def build(
    deck_path: Path = DECK_PATH,
    db_path: Path = DB_PATH,
    deck_name: str | None = DECK_NAME,
) -> dict:
    if not deck_path.exists():
        raise FileNotFoundError(f"Deck file not found: {deck_path}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    start = time.perf_counter()
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        cur = con.cursor()

        notes_batch: list[tuple] = []
        fts_batch: list[tuple] = []
        ntag_batch: list[tuple] = []
        ndeck_batch: list[tuple] = []
        tag_counts: dict[str, int] = {}
        deck_counts: dict[str, int] = {}

        BATCH = 1000
        n_notes = 0

        def flush():
            nonlocal notes_batch, fts_batch, ntag_batch, ndeck_batch
            if notes_batch:
                cur.executemany(
                    "INSERT OR REPLACE INTO notes "
                    "(guid, front, back, front_html, back_html, extras, tags_raw, deck, nid) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    notes_batch,
                )
                cur.executemany(
                    "INSERT INTO notes_fts (guid, front, back, tags) VALUES (?, ?, ?, ?)",
                    fts_batch,
                )
                if ntag_batch:
                    cur.executemany(
                        "INSERT OR IGNORE INTO note_tags (guid, tag) VALUES (?, ?)",
                        ntag_batch,
                    )
                if ndeck_batch:
                    cur.executemany(
                        "INSERT OR IGNORE INTO note_decks (guid, deck) VALUES (?, ?)",
                        ndeck_batch,
                    )
                notes_batch = []
                fts_batch = []
                ntag_batch = []
                ndeck_batch = []

        for note in iter_deck_notes(deck_path, deck_name=deck_name):
            primary_deck = note.decks[0] if note.decks else ""
            notes_batch.append((*note.to_row(), primary_deck, note.nid))
            fts_batch.append((note.guid, note.front, note.back, " ".join(note.tags)))
            for t in note.tags:
                ntag_batch.append((note.guid, t))
                tag_counts[t] = tag_counts.get(t, 0) + 1
            for d in note.decks:
                ndeck_batch.append((note.guid, d))
                deck_counts[d] = deck_counts.get(d, 0) + 1
            n_notes += 1
            if n_notes % BATCH == 0:
                flush()
        flush()

        cur.executemany(
            "INSERT OR REPLACE INTO tags (tag, count) VALUES (?, ?)",
            tag_counts.items(),
        )
        cur.executemany(
            "INSERT OR REPLACE INTO decks (deck, count) VALUES (?, ?)",
            deck_counts.items(),
        )

        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("deck_path", str(deck_path)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("deck_name", deck_name or ""),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("deck_mtime", str(deck_path.stat().st_mtime)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("note_count", str(n_notes)),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("tag_count", str(len(tag_counts))),
        )

        con.commit()
    finally:
        con.close()

    elapsed = time.perf_counter() - start
    return {
        "notes": n_notes,
        "tags": len(tag_counts),
        "seconds": round(elapsed, 2),
        "db_size_bytes": db_path.stat().st_size,
    }


def ensure_built(force: bool = False) -> dict | None:
    """Build only if needed. Returns build stats or None if cache was reused."""
    if not force and not needs_rebuild(DB_PATH, DECK_PATH):
        return None
    return build()


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Anki SQLite/FTS5 index.")
    ap.add_argument("--force", action="store_true", help="Rebuild even if up-to-date.")
    args = ap.parse_args()

    if not args.force and not needs_rebuild(DB_PATH, DECK_PATH):
        print(f"Index up-to-date at {DB_PATH}")
        return 0

    scope = f" (deck: {DECK_NAME})" if DECK_NAME else " (whole collection)"
    print(f"Building index from {DECK_PATH}{scope} ...")
    stats = build()
    print(
        f"Indexed {stats['notes']:,} notes, {stats['tags']:,} unique tags "
        f"in {stats['seconds']}s -> {DB_PATH} "
        f"({stats['db_size_bytes'] / 1_000_000:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
