"""Move a set of notes' cards into another Anki deck, with an undo log.

Flow: the UI (or the natural-language organizer) produces a list of Anki note
ids. `preview_move` reports what would happen; nothing changes until the user
confirms and `move_notes` runs. Before any write, every card's original deck is
recorded in a history file so `undo_move` can put it back.

All writes go through AnkiConnect (see anki_connect.py).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import Counter, OrderedDict
from pathlib import Path

from . import anki_connect as anki
from . import ingest, search
from .settings import data_dir

HISTORY_LIMIT = 50           # move batches kept for undo
MAX_NOTES = 20000            # safety bound on the notes one change acts on
MAX_KEEP_NOTES = 100000      # "everything except ..." lists: kept cards aren't changed
_DECK_NAME_MAX = 250
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_lock = threading.RLock()
PREVIEW_TTL = 600
PREVIEW_LIMIT = 100
_previews: OrderedDict[str, dict] = OrderedDict()


class StalePreview(ValueError):
    """The user must review the selection again before it can be changed."""


class ChangeUncertain(anki.AnkiConnectError):
    """Anki may have applied the write; its recovery record must be retained."""


def _collection() -> str:
    return str(Path(ingest.DECK_PATH).expanduser().resolve())


def _check_collection(expected: str | None = None) -> str:
    current = _collection()
    if expected is not None and expected != current:
        raise ValueError("This change belongs to a different collection. Select its profile in Setup first.")
    anki.check(collection_path=Path(current))
    return current


def _remember_preview(collection: str, selection: dict, snapshot: dict) -> str:
    with _lock:
        now = time.monotonic()
        for key in list(_previews):
            if now - _previews[key]["created"] >= PREVIEW_TTL:
                del _previews[key]
        while len(_previews) >= PREVIEW_LIMIT:
            _previews.popitem(last=False)
        token = uuid.uuid4().hex
        # Copy caller-owned lists, including nested card lists.
        _previews[token] = json.loads(json.dumps({
            "created": now, "collection": collection, "selection": selection, "snapshot": snapshot,
        }))
        return token


def _take_preview(token: str | None, collection: str, selection: dict) -> dict:
    # Called while holding _lock. One attempt consumes the preview, including
    # failed or uncertain writes, so a duplicate click cannot apply it twice.
    preview = _previews.pop(token, None) if isinstance(token, str) else None
    if (not preview or time.monotonic() - preview["created"] >= PREVIEW_TTL
            or preview["collection"] != collection or preview["selection"] != selection):
        raise StalePreview("This preview is missing, expired, or no longer matches. Preview the change again.")
    return preview["snapshot"]


def _require_snapshot(reviewed: dict, current: dict) -> None:
    if reviewed != current:
        raise StalePreview("The cards changed since the preview. Nothing was changed by this request. Preview again.")


def _uncertain(change_id: str, error: Exception) -> None:
    # The original pending record still exists if updating its status fails.
    try:
        _update_entry(change_id, status="uncertain")
    except OSError:
        pass
    raise ChangeUncertain(
        "Anki may have applied some or all of this change. Its recovery record is saved in "
        "Recent changes; check Anki or use Undo there before trying again."
    ) from error


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def clean_deck_name(raw: object) -> str:
    """Normalize a user-typed deck path ("GI", "Step 1 :: GI") or raise ValueError."""
    if not isinstance(raw, str):
        raise ValueError("Choose a deck to move the cards to.")
    name = raw.strip()
    if not name:
        raise ValueError("Choose a deck to move the cards to.")
    if _CONTROL_RE.search(name) or len(name) > _DECK_NAME_MAX:
        raise ValueError("That deck name isn't valid.")
    parts = [p.strip() for p in name.split("::")]
    if any(not p for p in parts):
        raise ValueError("Deck names can't have empty levels (check the '::' separators).")
    return "::".join(parts)


def clean_nids(raw: object, limit: int = MAX_NOTES) -> list[int]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("There are no cards to move.")
    nids = []
    for value in raw:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("Card list contained an invalid note id.") from None
        if n > 0:
            nids.append(n)
    nids = sorted(set(nids))
    if not nids:
        raise ValueError("There are no cards to move.")
    if len(nids) > limit:
        raise ValueError(f"That's more than {limit:,} notes. Narrow the search first.")
    return nids


def _deck_named_in(message: str, existing: list[str], name: str) -> str | None:
    """An existing deck the student named verbatim, when the model's name is a
    shortened form of it: the full path ("AnKing Step Deck::GI cards" passed
    as "AnKing Step Deck::GI") or a unique subdeck name ("GI subdeck" passed
    as "GI"). Longest match wins."""
    text = " " + " ".join(re.sub(r"[^\w:&]+", " ", (message or "").casefold().replace(" :: ", "::")).split()) + " "
    want = name.casefold()

    def said(phrase: str) -> bool:
        return f" {phrase} " in text

    by_path = [d for d in existing if "::" in d and said(d.casefold()) and d.casefold().startswith(want)]
    if by_path:
        return max(by_path, key=len)
    by_leaf = [d for d in existing
               if said(d.split("::")[-1].casefold()) and d.split("::")[-1].casefold().startswith(want)]
    longest = max((len(d.split("::")[-1]) for d in by_leaf), default=0)
    best = [d for d in by_leaf if len(d.split("::")[-1]) == longest]
    return best[0] if len(best) == 1 else None


def resolve_target(raw: object, existing: list[str], message: str = "") -> dict:
    """Match what the student called a deck ("my GI deck" -> "GI") to a real one.

    Exact (case-insensitive) path wins; then an existing deck path the
    student typed verbatim in `message`; then a UNIQUE deck whose last level
    matches (e.g. "GI" -> "AnKing Step Deck::GI"); otherwise the name is
    treated as a new deck, with close existing names offered as suggestions.
    """
    name = clean_deck_name(raw)
    # "my GI subdeck" means the GI subdeck: drop a leading possessive or
    # article when what's left names a real deck.
    first, _, rest = name.partition(" ")
    if rest and first.casefold() in ("my", "the", "our") and "::" not in first:
        stripped = resolve_target(rest, existing, message)
        if stripped["exists"]:
            return stripped
    return _resolve_target(name, existing, message)


def _resolve_target(name: str, existing: list[str], message: str) -> dict:
    import difflib

    by_path = {d.casefold(): d for d in existing}
    # What the student actually typed beats the model's (possibly shortened)
    # name, even when the shortened name is itself a real deck.
    typed = _deck_named_in(message, existing, name)
    if typed:
        return {"target": typed, "exists": True, "suggestions": []}
    if name.casefold() in by_path:
        return {"target": by_path[name.casefold()], "exists": True, "suggestions": []}
    leaf = name.split("::")[-1].casefold()
    same_leaf = [d for d in existing if d.split("::")[-1].casefold() == leaf]
    if "::" not in name and len(same_leaf) == 1:
        return {"target": same_leaf[0], "exists": True, "suggestions": []}
    # A new subdeck's own parents ("AnKing Step Deck" for "AnKing Step
    # Deck::GI") aren't alternatives the student might have meant.
    parts = name.casefold().split("::")
    ancestors = {"::".join(parts[:i]) for i in range(1, len(parts))}
    close = difflib.get_close_matches(name, existing, n=3, cutoff=0.6)
    # "GI" -> "AnKing Step Deck::GI subdeck": subdecks whose name starts with it.
    starts = [d for d in existing if d.split("::")[-1].casefold().startswith(leaf)]
    suggestions = [d for d in dict.fromkeys(same_leaf + starts + close)
                   if d.casefold() not in ancestors][:5]
    return {"target": name, "exists": False, "suggestions": suggestions}


def _outside_index_scope(target: str) -> bool:
    scope = (ingest.DECK_NAME or "").strip()
    return bool(scope) and not (target == scope or target.startswith(scope + "::"))


# ---------------------------------------------------------------------------
# History (undo log)
# ---------------------------------------------------------------------------


def history_path() -> Path:
    return data_dir() / "move_history.json"


def _read_history() -> list[dict]:
    try:
        data = json.loads(history_path().read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_history(entries: list[dict]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix="moves-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            recent_ids = {e["id"] for e in entries[-HISTORY_LIMIT:]}
            json.dump([e for e in entries if e["id"] in recent_ids
                       or e.get("status") in ("pending", "uncertain")], f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _update_entry(move_id: str, **changes) -> None:
    entries = _read_history()
    for e in entries:
        if e.get("id") == move_id:
            e.update(changes)
    _write_history(entries)


def list_moves(limit: int = 10) -> list[dict]:
    """Recent changes (moves, suspends), newest first, without per-card detail."""
    out = []
    entries = [e for e in _read_history() if e.get("collection") == _collection()]
    # Recovery records remain visible even after many subsequent changes.
    recent_ids = {e["id"] for e in entries[-limit:]}
    for e in reversed(entries):
        if e["id"] not in recent_ids and e.get("status") not in ("pending", "uncertain"):
            continue
        action = e.get("action", "move")
        cards = e.get("cards") or []
        out.append({k: e.get(k) for k in ("id", "created", "target", "label", "status")}
                   | {"action": action, "cards": len(cards),
                      "from_decks": sorted({c[2] for c in cards}) if action == "move" else []})
    return out


# ---------------------------------------------------------------------------
# Search-cache sync (so the sidebar and card deck labels reflect the move
# without a full re-index)
# ---------------------------------------------------------------------------


def _sync_cache(decks_by_nid: dict[int, set[str]]) -> None:
    try:
        con = search.get_conn()
        with con:
            for nid, decks in decks_by_nid.items():
                row = con.execute("SELECT guid FROM notes WHERE nid = ?", (nid,)).fetchone()
                if not row or not decks:
                    continue
                guid = row["guid"]
                con.execute("DELETE FROM note_decks WHERE guid = ?", (guid,))
                con.executemany("INSERT OR IGNORE INTO note_decks (guid, deck) VALUES (?, ?)",
                                [(guid, d) for d in decks])
                con.execute("UPDATE notes SET deck = ? WHERE guid = ?", (sorted(decks)[0], guid))
    except Exception:
        # The cache is a convenience; Anki already holds the truth.
        pass


# ---------------------------------------------------------------------------
# Preview / move / undo
# ---------------------------------------------------------------------------


def _plan(nids: list[int], target: str):
    by_note = anki.cards_by_note(nids)
    card_ids = [c for cards in by_note.values() for c in cards]
    current = anki.decks_of_cards(card_ids)
    to_move = [(cid, nid, current[cid]) for nid, cards in by_note.items()
               for cid in cards if cid in current and current[cid] != target]
    already = sum(1 for cid in card_ids if current.get(cid) == target)
    return by_note, to_move, already


def _move_snapshot(by_note: dict, to_move: list, already: int) -> dict:
    return {"cards": sorted([list(c) for c in to_move]), "already": already,
            "notes": sorted([[nid, sorted(cards)] for nid, cards in by_note.items()])}


def preview_move(nids: list[int], target: str) -> dict:
    """Describe a move without changing anything."""
    target = clean_deck_name(target)
    collection = _check_collection()
    by_note, to_move, already = _plan(nids, target)
    from_counts = Counter(deck for _, _, deck in to_move)
    target_exists = target in anki.deck_names()
    _check_collection(collection)
    token = _remember_preview(collection, {"action": "move", "nids": sorted(nids), "target": target},
                              _move_snapshot(by_note, to_move, already))
    return {
        "preview_id": token,
        "target": target,
        "target_exists": target_exists,
        "notes": len(by_note),
        "missing_notes": len(nids) - len(by_note),
        "cards_to_move": len(to_move),
        "already_in_target": already,
        "from_decks": [{"deck": d, "cards": n} for d, n in from_counts.most_common()],
        "outside_index_scope": _outside_index_scope(target),
        "index_scope": ingest.DECK_NAME or "",
    }


def move_notes(nids: list[int], target: str, *, preview_id: str | None = None, label: str = "") -> dict:
    """Move every card of `nids` into `target`, logging originals first."""
    target = clean_deck_name(target)
    with _lock:
        collection = _check_collection()
        reviewed = _take_preview(preview_id, collection,
                                 {"action": "move", "nids": sorted(nids), "target": target})
        by_note, to_move, already = _plan(nids, target)
        _require_snapshot(reviewed, _move_snapshot(by_note, to_move, already))
        if not to_move:
            return {"id": None, "moved": 0, "already_in_target": already, "target": target}
        move_id = uuid.uuid4().hex[:12]
        entry = {
            "id": move_id, "created": int(time.time()), "target": target,
            "label": str(label)[:200], "status": "pending", "collection": collection,
            # [card id, note id, original deck] — enough to restore exactly.
            "cards": [list(c) for c in to_move],
        }
        # Log BEFORE writing so even a crash mid-move can be undone.
        _write_history(_read_history() + [entry])
        try:
            if target not in anki.deck_names():
                _check_collection(collection)
                anki.create_deck(target)
            _check_collection(collection)
            anki.change_deck([cid for cid, _, _ in to_move], target)
            _check_collection(collection)
            actual = anki.decks_of_cards([cid for cid, _, _ in to_move])
            if any(actual.get(cid) != target for cid, _, _ in to_move):
                raise anki.AnkiConnectError("Couldn't verify all moved cards.")
            _update_entry(move_id, status="done")
        except Exception as error:
            _uncertain(move_id, error)
    _sync_cache({nid: {target} for nid in by_note})
    return {"id": move_id, "moved": len(to_move), "already_in_target": already,
            "notes": len(by_note), "target": target}


def undo_move(move_id: str) -> dict:
    """Return a batch's cards to their original decks.

    Only cards still sitting in the batch's target deck are moved back, so a
    card you have since moved elsewhere by hand is left alone.
    """
    with _lock:
        entry = next((e for e in _read_history() if e.get("id") == move_id), None)
        if entry is None:
            raise ValueError("That move is no longer in the history.")
        if entry.get("status") == "undone":
            raise ValueError("That change was already undone.")
        if not entry.get("collection"):
            raise ValueError("This older change has no saved profile identity and cannot be safely undone here.")
        _check_collection(entry["collection"])
        if entry.get("action") in CARD_ACTIONS:
            return _undo_card_action(entry)
        cards = [(int(c), int(n), str(d)) for c, n, d in entry["cards"]]
        current = anki.decks_of_cards([c for c, _, _ in cards])
        groups: dict[str, list[int]] = {}
        decks_by_nid: dict[int, set[str]] = {}
        skipped = 0
        for cid, nid, original in cards:
            if current.get(cid) == entry["target"]:
                groups.setdefault(original, []).append(cid)
                decks_by_nid.setdefault(nid, set()).add(original)
            else:
                skipped += 1
                if cid in current:
                    decks_by_nid.setdefault(nid, set()).add(current[cid])
        try:
            for deck, cids in groups.items():
                _check_collection(entry["collection"])
                anki.change_deck(cids, deck)
            _update_entry(move_id, status="undone", undone_at=int(time.time()))
        except Exception as error:
            _uncertain(move_id, error)
    _sync_cache(decks_by_nid)
    return {"id": move_id, "restored": sum(len(v) for v in groups.values()),
            "skipped": skipped, "decks": sorted(groups)}


# ---------------------------------------------------------------------------
# Suspend / unsuspend a set of cards (e.g. "in my GI deck, suspend everything
# except GERD"). Same pattern as moves: preview, user-confirmed apply, undo.
# ---------------------------------------------------------------------------

CARD_ACTIONS = ("suspend", "unsuspend")


def _search_term(value: str) -> str:
    """Quote a deck name for Anki's search syntax (where * and _ are wildcards)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")
    return f'"deck:{escaped}"'


def notes_in_deck(deck: str, nids: list[int], chunk: int = 1000) -> list[int]:
    """The subset of `nids` with a card in `deck` (or its subdecks), asked of
    Anki in chunks so the search string stays short."""
    found: set[int] = set()
    ids = sorted({int(n) for n in nids})
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        found.update(anki.invoke("findNotes", query=_search_term(deck) + " nid:" + ",".join(map(str, part))))
    return sorted(found)


def clean_card_spec(raw: dict) -> dict:
    """Validate {action, deck, nids, exclude_nids} from the UI.

    `nids` None means "every note in the deck"; a list restricts to those
    notes. `exclude_nids` are notes to leave alone. A deck or a note list is
    required so a request can never touch the whole collection by accident.
    """
    action = raw.get("action")
    if action not in CARD_ACTIONS:
        raise ValueError("Unknown card action.")
    deck = raw.get("deck")
    deck = clean_deck_name(deck) if deck else ""
    nids = None if raw.get("nids") is None else clean_nids(raw.get("nids"))
    exclude = ([] if not raw.get("exclude_nids")
               else clean_nids(raw.get("exclude_nids"), limit=MAX_KEEP_NOTES))
    if not deck and nids is None:
        raise ValueError("Choose a deck or a set of cards first.")
    return {"action": action, "deck": deck, "nids": nids, "exclude_nids": exclude}


def _card_query(spec: dict, extra: str = "") -> str:
    parts = []
    if spec["deck"]:
        parts.append(_search_term(spec["deck"]))
    if spec["nids"] is not None:
        parts.append("nid:" + ",".join(map(str, spec["nids"])))
    if spec["exclude_nids"]:
        parts.append("-nid:" + ",".join(map(str, spec["exclude_nids"])))
    if extra:
        parts.append(extra)
    return " ".join(parts)


def _cards_to_change(spec: dict) -> tuple[list[int], int]:
    """(cards whose state would change, cards already in that state)."""
    cards = anki.invoke("findCards", query=_card_query(spec))
    if not cards:
        return [], 0
    suspended = set(anki.invoke("findCards", query=_card_query(spec, "is:suspended")))
    want_suspended = spec["action"] == "suspend"
    change = [c for c in cards if (c in suspended) != want_suspended]
    return change, len(cards) - len(change)


def _kept_cards(spec: dict) -> int:
    """Cards in the deck deliberately left alone (the 'except ...' part)."""
    if not (spec["deck"] and spec["exclude_nids"]):
        return 0
    query = _search_term(spec["deck"]) + " nid:" + ",".join(map(str, spec["exclude_nids"]))
    return len(anki.invoke("findCards", query=query))


def preview_card_action(spec: dict) -> dict:
    collection = _check_collection()
    if spec["deck"] and spec["deck"] not in anki.deck_names():
        raise ValueError(f"There's no deck named {spec['deck']!r}.")
    change, already = _cards_to_change(spec)
    # Note ids for the preview's card lists: what changes, and what's kept.
    state = "-is:suspended" if spec["action"] == "suspend" else "is:suspended"
    change_nids = anki.invoke("findNotes", query=_card_query(spec, state)) if change else []
    kept_nids = []
    if spec["deck"] and spec["exclude_nids"]:
        kept_nids = anki.invoke("findNotes", query=_search_term(spec["deck"]) + " nid:"
                                + ",".join(map(str, spec["exclude_nids"])))
    kept = _kept_cards(spec)
    _check_collection(collection)
    token = _remember_preview(collection, spec, {"cards": sorted(change), "already": already})
    return {"preview_id": token, "action": spec["action"], "deck": spec["deck"], "cards_to_change": len(change),
            "already": already, "kept": kept,
            "change_nids": sorted(change_nids), "kept_nids": sorted(kept_nids)}


def _verify_card_state(cards: list[int], action: str, collection: str) -> None:
    # AnkiConnect versions differ in whether unsuspend returns True or null.
    # Verify actual card state instead of treating the return value as proof.
    _check_collection(collection)
    state = "is:suspended" if action == "suspend" else "-is:suspended"
    for i in range(0, len(cards), 1000):
        chunk = cards[i:i + 1000]
        query = "cid:" + ",".join(map(str, chunk)) + " " + state
        if set(anki.invoke("findCards", query=query)) != set(chunk):
            raise anki.AnkiConnectError("Couldn't verify all card changes.")
    _check_collection(collection)


def apply_card_action(spec: dict, *, preview_id: str | None = None, label: str = "") -> dict:
    with _lock:
        collection = _check_collection()
        reviewed = _take_preview(preview_id, collection, spec)
        if spec["deck"] and spec["deck"] not in anki.deck_names():
            raise ValueError(f"There's no deck named {spec['deck']!r}.")
        change, already = _cards_to_change(spec)
        _require_snapshot(reviewed, {"cards": sorted(change), "already": already})
        if not change:
            return {"id": None, "changed": 0, "already": already, "action": spec["action"]}
        action_id = uuid.uuid4().hex[:12]
        entry = {"id": action_id, "created": int(time.time()), "action": spec["action"],
                 "target": spec["deck"], "label": str(label)[:200], "status": "pending",
                 "cards": change, "collection": collection}
        _write_history(_read_history() + [entry])          # log before writing
        try:
            _check_collection(collection)
            anki.invoke(spec["action"], cards=change)
            _verify_card_state(change, spec["action"], collection)
            _update_entry(action_id, status="done")
        except Exception as error:
            _uncertain(action_id, error)
    return {"id": action_id, "changed": len(change), "already": already,
            "action": spec["action"], "deck": spec["deck"]}


def _undo_card_action(entry: dict) -> dict:
    """Reverse a suspend/unsuspend for cards still in the state it left them."""
    cards = [int(c) for c in entry["cards"]]
    query = "cid:" + ",".join(map(str, cards))
    existing = set(anki.invoke("findCards", query=query)) if cards else set()
    suspended = set(anki.invoke("findCards", query=query + " is:suspended")) if cards else set()
    if entry["action"] == "suspend":
        revert, opposite = [c for c in cards if c in suspended], "unsuspend"
    else:
        revert, opposite = [c for c in cards if c in existing and c not in suspended], "suspend"
    try:
        if revert:
            _check_collection(entry["collection"])
            anki.invoke(opposite, cards=revert)
            _verify_card_state(revert, opposite, entry["collection"])
        _update_entry(entry["id"], status="undone", undone_at=int(time.time()))
    except Exception as error:
        _uncertain(entry["id"], error)
    return {"id": entry["id"], "restored": len(revert), "skipped": len(cards) - len(revert),
            "decks": [entry.get("target") or ""]}
