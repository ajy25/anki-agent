"""Minimal AnkiConnect client — the only code path that writes to Anki.

AnkiConnect is an Anki add-on (code 2055492159) that exposes a JSON API on
http://127.0.0.1:8765 while Anki is running. Going through it means Anki itself
performs every change, so the collection stays consistent and syncs normally,
even while Anki is open. We never write to collection.anki2 directly.

Configure a non-default address with ANKICONNECT_URL.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterable

API_VERSION = 6
DEFAULT_URL = "http://127.0.0.1:8765"
TIMEOUT = 30.0
NID_CHUNK = 500  # note ids per findCards query, to keep the search string sane


class AnkiConnectError(RuntimeError):
    """AnkiConnect is unreachable or rejected a request."""


NOT_RUNNING = (
    "Couldn't reach Anki. Open Anki and make sure the AnkiConnect add-on "
    "(code 2055492159) is installed, then try again."
)


def _url() -> str:
    return (os.environ.get("ANKICONNECT_URL") or DEFAULT_URL).strip()


def invoke(action: str, *, timeout: float = TIMEOUT, **params: Any) -> Any:
    """Call one AnkiConnect action and return its `result`."""
    body = json.dumps({"action": action, "version": API_VERSION, "params": params}).encode()
    req = urllib.request.Request(_url(), data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise AnkiConnectError(NOT_RUNNING) from e
    except ValueError as e:
        raise AnkiConnectError("Anki returned an unreadable response.") from e
    if not isinstance(payload, dict) or "result" not in payload:
        raise AnkiConnectError("Anki returned an unexpected response. Update AnkiConnect.")
    if payload.get("error"):
        raise AnkiConnectError(f"Anki refused the request: {payload['error']}")
    return payload["result"]


def check(*, timeout: float = TIMEOUT) -> int:
    """Return the AnkiConnect API version, or raise if it is unusable."""
    version = invoke("version", timeout=timeout)
    if not isinstance(version, int) or version < API_VERSION:
        raise AnkiConnectError("Your AnkiConnect add-on is too old. Update it in Anki → Tools → Add-ons.")
    return version


def deck_names() -> list[str]:
    return sorted(invoke("deckNames"), key=str.casefold)


def cards_by_note(nids: Iterable[int]) -> dict[int, list[int]]:
    """Map note id -> its card ids (a cloze note has one card per cloze number).

    Notes that no longer exist in Anki are omitted.
    """
    wanted = sorted({int(n) for n in nids if int(n) > 0})
    out: dict[int, list[int]] = {}
    for i in range(0, len(wanted), NID_CHUNK):
        for info in invoke("notesInfo", notes=wanted[i : i + NID_CHUNK]) or []:
            if info and info.get("noteId") and info.get("cards"):
                out[int(info["noteId"])] = [int(c) for c in info["cards"]]
    return out


def decks_of_cards(card_ids: list[int]) -> dict[int, str]:
    """Map card id -> current deck name. Cards that no longer exist are omitted."""
    if not card_ids:
        return {}
    grouped = invoke("getDecks", cards=card_ids) or {}
    return {int(c): deck for deck, cards in grouped.items() for c in cards}


def create_deck(name: str) -> int:
    return invoke("createDeck", deck=name)


def change_deck(card_ids: list[int], deck: str) -> None:
    """Move cards to `deck` (Anki creates the deck if needed)."""
    if card_ids:
        invoke("changeDeck", cards=card_ids, deck=deck)
