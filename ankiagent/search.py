"""Query the indexed Anki deck.

Fast mode: BM25 over the FTS5 index, with a small query mini-language that
supports field prefixes and tag filters.

Smart mode lives in llm.py — it calls back into `fast_search` to gather
candidates, then reranks with the LLM.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .ingest import DB_PATH


# ---------------------------------------------------------------------------
# Tag display
# ---------------------------------------------------------------------------


def tag_label(tag: str) -> str:
    """Short, user-facing label for a tag: its last `::`-separated segment."""
    return tag.rsplit("::", 1)[-1]


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards in a needle (used with ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

# A user query may contain:
#   plain tokens      -> full-text match across front/back/tags
#   "quoted phrase"   -> phrase match
#   tag:<substring>   -> require the tag column to contain the substring
#   guid:<prefix>     -> match on GUID prefix (bypasses FTS)
#   front:<word>      -> match the front field only
#   back:<word>       -> match the back field only
#
# We translate these into FTS5 syntax. Unrecognized prefixes are treated as
# plain tokens.

_FIELD_PREFIXES = {"front", "back", "tags"}
_TAG_SAFE_RE = re.compile(r"[^A-Za-z0-9_:/#\-]")

# Drop common English function words so a conversational query like
# "drug that causes bradycardia" reduces to its medical terms under FTS5's
# implicit AND semantics.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "of", "in", "on",
    "at", "to", "from", "by", "for", "with", "without", "into", "onto",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "done",
    "has", "have", "had", "having",
    "can", "could", "would", "should", "will", "shall", "may", "might", "must",
    "that", "this", "these", "those", "there", "here",
    "which", "what", "who", "whom", "whose", "when", "where", "why", "how",
    "it", "its", "as", "also",
    "i", "we", "you", "he", "she", "they", "them", "me", "us",
    "not", "no", "nor",
    "cause", "causes", "caused", "causing",
    "about", "some", "any", "all", "such", "than", "very", "so",
})


@dataclass
class ParsedQuery:
    fts: str                    # compiled FTS5 MATCH string (empty = no text filter)
    tag_filters: list[str]      # tag substrings (AND)
    guid_prefix: str | None     # optional GUID prefix filter


def _tokenize(q: str) -> list[str]:
    """Tokenize a user query, preserving "quoted phrases" as single tokens."""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in q:
        if ch == '"':
            if in_quote:
                out.append('"' + "".join(buf) + '"')
                buf = []
                in_quote = False
            else:
                if buf:
                    out.append("".join(buf))
                    buf = []
                in_quote = True
        elif ch.isspace() and not in_quote:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append(('"' + "".join(buf) + '"') if in_quote else "".join(buf))
    return [t for t in out if t]


def _sanitize_fts_token(tok: str) -> str:
    """Make a token safe for FTS5 MATCH.

    FTS5 has a handful of operator characters. For user-typed plain tokens we
    drop anything that could confuse the parser and wrap in quotes. Phrase
    tokens (already quoted by the user) are passed through after escaping.
    """
    if tok.startswith('"') and tok.endswith('"'):
        inner = tok[1:-1].replace('"', '""')
        return f'"{inner}"'
    cleaned = re.sub(r'[^A-Za-z0-9]+', " ", tok).strip()
    if not cleaned:
        return ""
    # Individual words inside are OR'd by adjacency in a phrase, but we
    # actually want AND between them. Return as separate quoted words joined
    # by space so FTS treats them as an implicit AND.
    parts = cleaned.split()
    return " ".join(f'"{p}"' for p in parts)


def parse_query(q: str) -> ParsedQuery:
    q = q.strip()
    if not q:
        return ParsedQuery(fts="", tag_filters=[], guid_prefix=None)

    tokens = _tokenize(q)
    fts_parts: list[str] = []
    tag_filters: list[str] = []
    guid_prefix: str | None = None

    for tok in tokens:
        low = tok.lower()
        if low.startswith("tag:"):
            val = tok[4:].strip('"')
            val = _TAG_SAFE_RE.sub("", val)
            if val:
                tag_filters.append(val)
            continue
        if low.startswith("guid:"):
            val = tok[5:].strip('"')
            val = re.sub(r"[^A-Za-z0-9\-]", "", val)
            if val:
                guid_prefix = val
            continue
        # field:value
        if ":" in tok and not tok.startswith('"'):
            field, _, rest = tok.partition(":")
            if field.lower() in _FIELD_PREFIXES and rest:
                safe = _sanitize_fts_token(rest)
                if safe:
                    fts_parts.append(f"{field.lower()}:({safe})")
                continue
        # Drop function-word-only tokens (but keep multi-word phrases intact).
        if not tok.startswith('"') and low in _STOPWORDS:
            continue
        safe = _sanitize_fts_token(tok)
        if safe:
            fts_parts.append(safe)

    return ParsedQuery(
        fts=" ".join(fts_parts),
        tag_filters=tag_filters,
        guid_prefix=guid_prefix,
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_CONN: sqlite3.Connection | None = None


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        con = sqlite3.connect(db_path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _CONN = con
    return _CONN


# ---------------------------------------------------------------------------
# Fast search
# ---------------------------------------------------------------------------

SNIPPET_SQL = "snippet(notes_fts, {col}, '<mark>', '</mark>', ' … ', 16)"


def _tags_by_guid(
    con: sqlite3.Connection, guids: list[str]
) -> dict[str, list[str]]:
    """Batch-fetch the tag list for many notes in one (chunked) query."""
    out: dict[str, list[str]] = {}
    if not guids:
        return out
    for i in range(0, len(guids), 900):   # stay under SQLite's variable cap
        chunk = guids[i : i + 900]
        placeholders = ",".join("?" * len(chunk))
        for r in con.execute(
            f"SELECT guid, tag FROM note_tags WHERE guid IN ({placeholders})",
            chunk,
        ):
            out.setdefault(r["guid"], []).append(r["tag"])
    return out


def _decks_by_guid(
    con: sqlite3.Connection, guids: list[str]
) -> dict[str, list[str]]:
    """Batch-fetch the deck path(s) for many notes in one (chunked) query."""
    out: dict[str, list[str]] = {}
    if not guids:
        return out
    for i in range(0, len(guids), 900):
        chunk = guids[i : i + 900]
        placeholders = ",".join("?" * len(chunk))
        for r in con.execute(
            f"SELECT guid, deck FROM note_decks WHERE guid IN ({placeholders})",
            chunk,
        ):
            out.setdefault(r["guid"], []).append(r["deck"])
    return out


def _hydrate(con: sqlite3.Connection, guids: list[str]) -> dict[str, dict]:
    if not guids:
        return {}
    placeholders = ",".join("?" * len(guids))
    rows = con.execute(
        f"""SELECT guid, front, back, front_html, back_html, extras, tags_raw, deck, nid
            FROM notes WHERE guid IN ({placeholders})""",
        guids,
    ).fetchall()
    out = {}
    for r in rows:
        tag_list = r["tags_raw"].split() if r["tags_raw"] else []
        keys = r.keys()
        out[r["guid"]] = {
            "guid": r["guid"],
            "nid": r["nid"] if "nid" in keys else 0,
            "front": r["front"],
            "back": r["back"],
            "front_html": r["front_html"],
            "back_html": r["back_html"],
            "extras_html": json.loads(r["extras"]),
            "tags_raw": r["tags_raw"],
            "tags": tag_list,
            "deck": r["deck"] if "deck" in keys else "",
        }
    return out


def fast_search(
    query: str,
    *,
    limit: int = 50,
    tags: Iterable[str] = (),
    exact_tags: Iterable[str] = (),
    any_tags: Iterable[str] = (),
    any_keywords: Iterable[str] = (),
    deck: str = "",
) -> list[dict]:
    """Return a ranked list of notes matching the query.

    `tags` — substring tag filters (also gathered from `tag:` operators in
    the query string). A note passes if each substring appears somewhere in
    its tags (AND across filters).

    `exact_tags` — full-path tag strings that must be present verbatim on
    the note (AND across filters). Used by sidebar/chip clicks where we
    want precise filtering (e.g. so "Only_Step_1" doesn't also match
    "Only_Step_1&2_Overlap").

    `any_tags` — an OR group of tag substrings: a note passes if AT LEAST
    one of them appears in its tags. ANDed with the other tag filters.

    `any_keywords` — an OR group of free-text terms: a note matches if it
    contains AT LEAST one of them. Compiled into the FTS query and ANDed with
    `query`'s terms.

    `deck` — restrict to a deck subtree: a note passes if one of its decks is
    this exact path or a subdeck of it. Empty = no deck restriction.
    """
    con = get_conn()
    parsed = parse_query(query)
    # Merge UI substring filters with in-query ones.
    sub_tags = list(parsed.tag_filters) + [t for t in tags if t]
    exact_tags = [t for t in exact_tags if t]
    any_tags = [t for t in any_tags if t]
    any_keywords = [k for k in (str(k).strip() for k in any_keywords) if k]
    deck = (deck or "").strip()
    has_tag_filter = bool(sub_tags or exact_tags or any_tags)
    need_post = has_tag_filter or bool(deck)

    # Compile the FTS MATCH string: parsed query terms (implicit AND) plus an
    # OR group built from any_keywords, the two ANDed together.
    fts = parsed.fts
    or_parts: list[str] = []
    for kw in any_keywords:
        tok = _sanitize_fts_token(kw)
        if tok:
            or_parts.append(f"({tok})" if " " in tok else tok)
    if or_parts:
        any_clause = "(" + " OR ".join(or_parts) + ")"
        fts = f"{fts} {any_clause}".strip() if fts else any_clause

    def passes_tags(note_tags: list[str]) -> bool:
        if not all(t in note_tags for t in exact_tags):
            return False
        if sub_tags or any_tags:
            joined = " ".join(note_tags).lower()
            if not all(t.lower() in joined for t in sub_tags):
                return False
            if any_tags and not any(t.lower() in joined for t in any_tags):
                return False
        return True

    def deck_ok(note_decks: list[str]) -> bool:
        if not deck:
            return True
        return any(d == deck or d.startswith(deck + "::") for d in note_decks)

    def keep(g: str, tags_by: dict, decks_by: dict) -> bool:
        if has_tag_filter and not passes_tags(tags_by.get(g, [])):
            return False
        if deck and not deck_ok(decks_by.get(g, [])):
            return False
        return True

    # Special case: guid prefix search bypasses FTS entirely.
    if parsed.guid_prefix and not fts and not need_post:
        rows = con.execute(
            "SELECT guid FROM notes WHERE guid LIKE ? LIMIT ?",
            (parsed.guid_prefix + "%", limit),
        ).fetchall()
        guids = [r["guid"] for r in rows]
        full = _hydrate(con, guids)
        return [
            {**full[g], "score": 0.0, "snippet_front": "", "snippet_back": ""}
            for g in guids
            if g in full
        ]

    # No full-text query: list by tag and/or deck.
    if not fts:
        if exact_tags and not sub_tags and not any_tags and not deck:
            # All-exact, no deck: clean INTERSECT.
            placeholders = ",".join("?" * len(exact_tags))
            sql = f"""
                SELECT guid FROM note_tags WHERE tag IN ({placeholders})
                GROUP BY guid
                HAVING COUNT(DISTINCT tag) = ?
                LIMIT ?
            """
            rows = con.execute(
                sql, list(exact_tags) + [len(exact_tags), limit]
            ).fetchall()
            guids = [r["guid"] for r in rows]
            full = _hydrate(con, guids)
            return [
                {**full[g], "score": 0.0, "snippet_front": "", "snippet_back": ""}
                for g in guids if g in full
            ]
        if has_tag_filter or deck:
            # Pull candidate guids by tag (preferred) or by deck, then filter.
            if has_tag_filter:
                any_pattern = sub_tags + exact_tags + any_tags
                placeholders = " OR ".join(["t.tag LIKE ?"] * len(any_pattern))
                rows = con.execute(
                    f"SELECT DISTINCT t.guid FROM note_tags t WHERE {placeholders} LIMIT ?",
                    [f"%{p}%" for p in any_pattern] + [limit * 5],
                ).fetchall()
            else:
                esc = _like_escape(deck)
                rows = con.execute(
                    "SELECT DISTINCT guid FROM note_decks "
                    "WHERE deck = ? OR deck LIKE ? ESCAPE '\\' LIMIT ?",
                    (deck, esc + "::%", limit * 5),
                ).fetchall()
            candidate_guids = [r["guid"] for r in rows]
            tags_by = _tags_by_guid(con, candidate_guids) if has_tag_filter else {}
            decks_by = _decks_by_guid(con, candidate_guids) if deck else {}
            filtered = [g for g in candidate_guids if keep(g, tags_by, decks_by)]
            filtered = filtered[:limit]
            full = _hydrate(con, filtered)
            return [
                {**full[g], "score": 0.0, "snippet_front": "", "snippet_back": ""}
                for g in filtered if g in full
            ]
        return []

    # FTS path.
    fts_sql = f"""
        SELECT guid,
               {SNIPPET_SQL.format(col=1)} AS snippet_front,
               {SNIPPET_SQL.format(col=2)} AS snippet_back,
               bm25(notes_fts) AS score
        FROM notes_fts
        WHERE notes_fts MATCH ?
        ORDER BY bm25(notes_fts)
        LIMIT ?
    """
    # Fetch a wider pool so we can apply tag/deck filters without running short.
    pool = max(limit * 3, 150) if need_post else limit
    try:
        rows = con.execute(fts_sql, (fts, pool)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    # OR fallback: if strict AND produces nothing and the query has multiple
    # terms, retry with OR so partial matches can surface.
    if not rows and " " in fts and '"' in fts:
        or_fts = re.sub(r'"\s+"', '" OR "', fts)
        try:
            rows = con.execute(fts_sql, (or_fts, pool)).fetchall()
        except sqlite3.OperationalError:
            rows = []

    results: list[dict] = []
    guids_in_order: list[str] = []
    meta_by_guid: dict[str, sqlite3.Row] = {}
    for r in rows:
        guids_in_order.append(r["guid"])
        meta_by_guid[r["guid"]] = r

    # Apply tag and/or deck filters if any were requested.
    if need_post and guids_in_order:
        tags_by = _tags_by_guid(con, guids_in_order) if has_tag_filter else {}
        decks_by = _decks_by_guid(con, guids_in_order) if deck else {}
        guids_in_order = [
            g for g in guids_in_order if keep(g, tags_by, decks_by)
        ][:limit]
    else:
        guids_in_order = guids_in_order[:limit]

    if not guids_in_order:
        return []

    hydrated = _hydrate(con, guids_in_order)
    for g in guids_in_order:
        base = hydrated.get(g)
        if not base:
            continue
        m = meta_by_guid[g]
        results.append({
            **base,
            "score": float(m["score"]),
            "snippet_front": m["snippet_front"],
            "snippet_back": m["snippet_back"],
        })
    return results


def get_note(guid: str) -> dict | None:
    con = get_conn()
    full = _hydrate(con, [guid])
    return full.get(guid)


def stats() -> dict:
    con = get_conn()
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    return {
        "notes": int(meta.get("note_count") or 0),
        "tags": int(meta.get("tag_count") or 0),
        # The deck the index was built from; empty means the whole collection.
        "deck": (meta.get("deck_name") or "").strip(),
    }


def deck_tree() -> list[dict]:
    """Build the nested deck hierarchy for the sidebar selector.

    Returns a list of root nodes, each {path, label, count, children}, where
    `count` is the inclusive note count (the deck plus all its subdecks). Empty
    when the source has no deck metadata (e.g. a TSV export).
    """
    con = get_conn()
    exact = {
        r["deck"]: int(r["count"])
        for r in con.execute("SELECT deck, count FROM decks")
        if r["deck"]
    }
    if not exact:
        return []

    # Materialize a node for every deck path AND every ancestor path.
    nodes: dict[str, dict] = {}
    for path in exact:
        parts = path.split("::")
        for i in range(1, len(parts) + 1):
            p = "::".join(parts[:i])
            nodes.setdefault(
                p, {"label": parts[i - 1], "own": 0, "total": 0, "children": set()}
            )
        for i in range(2, len(parts) + 1):
            nodes["::".join(parts[: i - 1])]["children"].add("::".join(parts[:i]))
    for path, c in exact.items():
        nodes[path]["own"] = c

    # Inclusive totals: process deepest paths first so children are ready.
    for p in sorted(nodes, key=lambda s: s.count("::"), reverse=True):
        n = nodes[p]
        n["total"] = n["own"] + sum(nodes[c]["total"] for c in n["children"])

    def build(p: str) -> dict:
        n = nodes[p]
        kids = sorted(n["children"], key=lambda c: nodes[c]["label"].lower())
        return {
            "path": p,
            "label": n["label"],
            "count": n["total"],
            "children": [build(c) for c in kids],
        }

    roots = [p for p in nodes if "::" not in p]
    return [build(p) for p in sorted(roots, key=lambda p: nodes[p]["label"].lower())]


def top_tags(limit: int = 200) -> list[tuple[str, int]]:
    con = get_conn()
    rows = con.execute(
        "SELECT tag, count FROM tags ORDER BY count DESC LIMIT ?", (limit,)
    ).fetchall()
    return [(r["tag"], int(r["count"])) for r in rows]


def find_tags(substring: str, limit: int = 25) -> list[dict]:
    """Tag-discovery helper for AI-assisted search.

    Returns up to `limit` distinct tags whose path contains `substring`
    (case-insensitive), most-used first, as [{tag, count}]. The deck has
    ~40k tags, so callers locate real tag paths by probing substrings
    rather than enumerating everything.
    """
    s = (substring or "").strip()
    if not s:
        return []
    con = get_conn()
    # Escape LIKE wildcards in the user/LLM-supplied needle.
    esc = _like_escape(s)
    rows = con.execute(
        "SELECT tag, count FROM tags "
        "WHERE tag LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "ORDER BY count DESC LIMIT ?",
        (f"%{esc}%", max(1, min(limit, 60))),
    ).fetchall()
    return [{"tag": r["tag"], "count": int(r["count"])} for r in rows]


def sidebar_tags(limit: int = 40) -> list[dict]:
    """Most-used tags in the deck, for the sidebar quick-filter list.

    Deck-agnostic: no hand-picked tags. Each entry is the full tag path, a
    short label (last path segment), and its note count.
    """
    return [
        {"tag": tag, "label": tag_label(tag), "count": count}
        for tag, count in top_tags(limit)
    ]
