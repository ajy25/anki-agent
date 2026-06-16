"""Parse the AnKing TSV export into structured note records.

The deck file is a tab-separated export from Anki:
  - First few lines are `#key:value` metadata comments.
  - Each subsequent line is one note: 19 tab-separated fields.
  - Field 18 (index 17) = note GUID.
  - Field 19 (index 18) = space-separated hierarchical tags.
  - Fields 1..17 contain HTML with cloze deletions and Anki-specific markup.

We also expose helpers to clean HTML for full-text search and to render
display HTML with images stripped.
"""

from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator


# Anki cloze syntax: {{c1::answer}} or {{c1::answer::hint}}
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", re.DOTALL)
# Anki sound tokens: [sound:foo.mp3]
SOUND_RE = re.compile(r"\[sound:[^\]]+\]")
# Anki type-in tokens: [type:foo]
TYPE_RE = re.compile(r"\[type:[^\]]+\]")


@dataclass
class Note:
    guid: str
    front: str           # plain text for search
    back: str            # plain text for search
    front_html: str      # sanitized HTML for display
    back_html: str       # sanitized HTML for display
    extras_html: list[str] = field(default_factory=list)   # fields 3..17, sanitized
    tags_raw: str = ""
    tags: list[str] = field(default_factory=list)
    # Full hierarchical deck path(s) this note's cards live in, "::" between
    # levels (e.g. "AnKing Step Deck::Review::5. Hematology"). A note can span
    # more than one deck. Empty for TSV exports (no deck metadata).
    decks: list[str] = field(default_factory=list)
    # Anki's numeric note id (notes.id) — searchable in the Anki browser as
    # `nid:<id>`. 0 for TSV exports, which carry no note id.
    nid: int = 0

    def to_row(self) -> tuple:
        return (
            self.guid,
            self.front,
            self.back,
            self.front_html,
            self.back_html,
            json.dumps(self.extras_html, ensure_ascii=False),
            self.tags_raw,
        )

    def to_dict(self) -> dict:
        return asdict(self)


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text. Drops tags, keeps text content."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0   # inside <script>/<style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self._chunks.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("p", "div", "li", "tr"):
            self._chunks.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(s: str) -> str:
    """Strip HTML and Anki markup, returning whitespace-normalized plain text."""
    if not s:
        return ""
    # Flatten cloze first so the answer text remains.
    s = CLOZE_RE.sub(r"\1", s)
    s = SOUND_RE.sub(" ", s)
    s = TYPE_RE.sub(" ", s)
    parser = _HTMLToText()
    try:
        parser.feed(s)
    except Exception:
        # Malformed HTML — fall back to a regex strip.
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
    text = parser.text()
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Tags allowed when rendering note HTML. We DROP <img> entirely (per user request)
# and strip scripts/styles. Bleach is applied later for safety; this list is the
# whitelist passed to bleach in app.py.
DISPLAY_ALLOWED_TAGS = {
    "a", "b", "i", "em", "strong", "u", "s", "sub", "sup",
    "div", "span", "p", "br", "hr",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "code", "pre",
    "mark",
}
DISPLAY_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "span": ["class"],
    "div": ["class"],
    "mark": ["data-cloze"],
}


def render_html(s: str) -> str:
    """Convert raw Anki HTML to display HTML.

    - Strip <img> and <audio> entirely.
    - Convert cloze {{c1::X}} -> <mark data-cloze="1">X</mark>.
    - Drop [sound:...] tokens.
    - Leave other tags for downstream sanitization (bleach in app.py).
    """
    if not s:
        return ""
    s = re.sub(r"<img\b[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<audio\b[^>]*>.*?</audio>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<source\b[^>]*>", "", s, flags=re.IGNORECASE)
    s = SOUND_RE.sub("", s)
    s = TYPE_RE.sub("", s)

    def cloze_repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        # Recursively render any nested HTML inside the cloze answer.
        return f'<mark data-cloze="1">{inner}</mark>'

    s = CLOZE_RE.sub(cloze_repl, s)
    return s


def split_tags(tags_raw: str) -> list[str]:
    if not tags_raw:
        return []
    return [t for t in tags_raw.split() if t]


def iter_notes(path: str | Path) -> Iterator[Note]:
    """Yield Note records from an AnKing TSV export."""
    p = Path(path)
    with p.open("r", encoding="utf-8", newline="") as f:
        # Skip leading `#key:value` metadata lines.
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                return
            if not line.startswith("#"):
                f.seek(pos)
                break

        # csv handles tab-quoted fields per the Anki export convention.
        reader = csv.reader(f, delimiter="\t", quotechar='"')
        for row in reader:
            if len(row) < 19:
                # Pad short rows so indexing is safe.
                row = row + [""] * (19 - len(row))
            front_raw = row[0]
            back_raw = row[1]
            extras_raw = row[2:17]
            guid = row[17].strip()
            tags_raw = row[18].strip()
            if not guid:
                continue
            yield Note(
                guid=guid,
                front=html_to_text(front_raw),
                back=html_to_text(back_raw),
                front_html=render_html(front_raw),
                back_html=render_html(back_raw),
                extras_html=[render_html(x) for x in extras_raw if x.strip()],
                tags_raw=tags_raw,
                tags=split_tags(tags_raw),
            )


# --- Live Anki collection (.anki2) ------------------------------------------
#
# Instead of a static TSV export, we can read the student's actual Anki
# collection on disk (e.g. ~/Library/Application Support/Anki2/<profile>/
# collection.anki2). It's an SQLite database. We select notes by *deck name*
# (the hierarchical name shown in Anki's sidebar, e.g. "AnKing Step Deck"),
# which selects that deck and all of its subdecks. This is deck-agnostic: it
# works for any deck, not just AnKing. Each note's fields are mapped onto our
# Note by name (Text/Front -> front, Extra/Back -> back, the rest -> extras),
# with a positional fallback so arbitrary note types still parse.

# Anki separator used both inside notes.flds AND between deck-name levels.
_ANKI_FIELD_SEP = "\x1f"
# SQLite file magic — lets us tell a real collection from a TSV export.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Field-name preferences when mapping an arbitrary note type onto front/back.
# First present name wins; otherwise we fall back to field position.
_FRONT_FIELD_NAMES = ("Text", "Front", "Question", "Header")
_BACK_FIELD_NAMES = ("Extra", "Back Extra", "Back", "Answer")
# Fields that are never useful as searchable content.
_SKIP_FIELD_NAMES = {"ankihub_id"}


def looks_like_anki_collection(path: str | Path) -> bool:
    """True if `path` is an SQLite database (an Anki collection), not a TSV."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _open_anki_collection(path: str | Path) -> tuple[sqlite3.Connection, Path]:
    """Snapshot the live collection to a temp dir and open it read-only.

    Anki may hold a write lock or have an un-checkpointed WAL while running, so
    we copy `collection.anki2` (+ its -wal/-shm sidecars) before touching it —
    we never read or mutate the live file directly. Caller must close the
    connection and remove the returned temp directory.
    """
    src = Path(path)
    tmpdir = Path(tempfile.mkdtemp(prefix="ankiagent_deck_"))
    tmp = tmpdir / "collection.anki2"
    shutil.copy2(src, tmp)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(tmp) + suffix))
    con = sqlite3.connect(str(tmp))
    # Anki indexes use a custom "unicase" collation that bare sqlite3 lacks;
    # register a stand-in so queries against notetypes/fields don't error.
    con.create_collation("unicase", lambda a, b: (a > b) - (a < b))
    con.row_factory = sqlite3.Row
    return con, tmpdir


def list_anki_decks(path: str | Path) -> list[str]:
    """Return the distinct top-level deck names in a collection (for messages)."""
    con, tmpdir = _open_anki_collection(path)
    try:
        roots = {
            r["name"].split(_ANKI_FIELD_SEP, 1)[0]
            for r in con.execute("SELECT name FROM decks")
        }
        roots.discard("Default")
        return sorted(roots)
    finally:
        con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _map_fields(names: dict[int, str], parts: list[str]) -> tuple[str, str, list[str]]:
    """Split a note's fields into (front, back, extras) by name, then position."""
    by_name = {names.get(i, str(i)): parts[i] for i in range(len(parts))}

    front_key = next((n for n in _FRONT_FIELD_NAMES if n in by_name), None)
    if front_key is None:
        front_key = names.get(0, "0")
    back_key = next(
        (n for n in _BACK_FIELD_NAMES if n in by_name and n != front_key), None
    )
    if back_key is None:
        # First field that isn't the front and isn't noise.
        back_key = next(
            (
                names.get(i)
                for i in range(1, len(parts))
                if names.get(i) not in (front_key, *_SKIP_FIELD_NAMES)
            ),
            None,
        )

    front_raw = by_name.get(front_key, "")
    back_raw = by_name.get(back_key, "") if back_key else ""
    extras_raw = [
        v
        for k, v in by_name.items()
        if k not in (front_key, back_key) and k not in _SKIP_FIELD_NAMES and v.strip()
    ]
    return front_raw, back_raw, extras_raw


def iter_notes_anki2(
    path: str | Path, deck_name: str | None = None
) -> Iterator[Note]:
    """Yield Note records straight from a live Anki collection (.anki2).

    If `deck_name` is given, only notes with a card in that deck (or any of its
    subdecks) are yielded; the name is the one shown in Anki, with `::` between
    levels (e.g. "AnKing Step Deck" or "AnKing Step Deck::Review"). If omitted,
    the whole collection is indexed. Fields are mapped onto front/back/extras by
    name with a positional fallback, so any note type parses.
    """
    con, tmpdir = _open_anki_collection(path)
    try:
        # field name keyed by (notetype id, ord)
        fields: dict[int, dict[int, str]] = {}
        for r in con.execute("SELECT ntid, ord, name FROM fields"):
            fields.setdefault(r["ntid"], {})[r["ord"]] = r["name"]

        # Deck display name (with "::" between levels) keyed by deck id.
        deck_name_by_id = {
            r["id"]: r["name"].replace(_ANKI_FIELD_SEP, "::")
            for r in con.execute("SELECT id, name FROM decks")
        }

        if deck_name:
            needle = deck_name.strip().replace("::", _ANKI_FIELD_SEP)
            deck_ids = [
                r["id"]
                for r in con.execute("SELECT id, name FROM decks")
                if r["name"] == needle or r["name"].startswith(needle + _ANKI_FIELD_SEP)
            ]
            if not deck_ids:
                roots = sorted(
                    {
                        r["name"].split(_ANKI_FIELD_SEP, 1)[0]
                        for r in con.execute("SELECT name FROM decks")
                    }
                    - {"Default"}
                )
                raise ValueError(
                    f"No Anki deck named {deck_name!r}. Available decks: "
                    + ", ".join(repr(x) for x in roots)
                )
            ph = ",".join("?" * len(deck_ids))
            query = (
                "SELECT DISTINCT n.id AS nid, n.guid, n.flds, n.tags, n.mid "
                "FROM notes n JOIN cards c ON c.nid = n.id "
                f"WHERE c.did IN ({ph})"
            )
            rows = con.execute(query, deck_ids).fetchall()
            card_rows = con.execute(
                "SELECT n.guid AS guid, c.did AS did "
                "FROM notes n JOIN cards c ON c.nid = n.id "
                f"WHERE c.did IN ({ph})",
                deck_ids,
            )
        else:
            rows = con.execute(
                "SELECT id AS nid, guid, flds, tags, mid FROM notes"
            ).fetchall()
            card_rows = con.execute(
                "SELECT n.guid AS guid, c.did AS did "
                "FROM notes n JOIN cards c ON c.nid = n.id"
            )

        # Map each note guid -> the set of deck paths its cards live in (scoped
        # to the selected deck subtree when deck_name is given).
        decks_by_guid: dict[str, set[str]] = {}
        for cr in card_rows:
            nm = deck_name_by_id.get(cr["did"])
            if nm:
                decks_by_guid.setdefault(cr["guid"], set()).add(nm)

        for r in rows:
            names = fields.get(r["mid"], {})
            parts = (r["flds"] or "").split(_ANKI_FIELD_SEP)
            front_raw, back_raw, extras_raw = _map_fields(names, parts)
            guid = (r["guid"] or "").strip()
            tags_raw = (r["tags"] or "").strip()
            if not guid:
                continue
            yield Note(
                guid=guid,
                front=html_to_text(front_raw),
                back=html_to_text(back_raw),
                front_html=render_html(front_raw),
                back_html=render_html(back_raw),
                extras_html=[render_html(x) for x in extras_raw],
                tags_raw=tags_raw,
                tags=split_tags(tags_raw),
                decks=sorted(decks_by_guid.get(guid, ())),
                nid=int(r["nid"] or 0),
            )
    finally:
        con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def iter_deck_notes(path: str | Path, deck_name: str | None = None) -> Iterator[Note]:
    """Yield Note records from either a live Anki collection or a TSV export.

    Dispatches on file content: an SQLite database is read as a collection
    (.anki2, filtered by `deck_name`), anything else is parsed as an AnKing TSV
    text export (.txt) — TSV exports have no deck metadata, so `deck_name` is
    ignored for them.
    """
    p = Path(path)
    if p.suffix.lower() == ".apkg":
        raise ValueError(
            f"{p} is an .apkg package. Import it into Anki first, then point "
            "DECK_PATH at your profile's collection.anki2."
        )
    if looks_like_anki_collection(p):
        yield from iter_notes_anki2(p, deck_name=deck_name)
    else:
        yield from iter_notes(p)


def collect_tag_counts(notes: Iterable[Note]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n in notes:
        for t in n.tags:
            counts[t] = counts.get(t, 0) + 1
    return counts
