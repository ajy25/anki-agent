"""LLM-assisted retrieval and chat.

Agentic chat (multi-turn, tool-using loop):
  The model decides what to do. It has tools for deck search and full-note
  lookup, and iterates (calling tools repeatedly as needed) until it has
  enough evidence to answer. Prior conversation turns are passed in so
  follow-up questions have context. Each unique card it observes gets a
  stable `[N]` reference so the final answer can cite it.

Plain tutor chat (multi-turn, no tools, no deck access).

Provider and model are configured in .env (see llm_client.py).
"""

from __future__ import annotations

import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Iterable

from . import search
from .llm_client import MODEL, get_client, get_model, reasoning_kwargs, reasoning_output_kwargs

RERANK_TIMEOUT = 20.0


class TutorCancelled(Exception):
    """The client discarded its tutor request."""


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled.is_set():
        raise TutorCancelled()


RATE_LIMIT_RETRIES = 4        # extra tries for one request after a 429
RATE_LIMIT_MAX_WAIT = 30.0    # longest wait worth sitting through; longer fails fast
TRANSIENT_RETRIES = 1         # extra tries after a 5xx / dropped connection
_RETRY_IN_RE = re.compile(r"try again in ([\d.hms]+)", re.IGNORECASE)
_DURATION_RE = re.compile(r"([\d.]+)(ms|h|m|s)")
_DAILY_RE = re.compile(r"per day|\((?:TPD|RPD)\)", re.IGNORECASE)


class RateLimitExhausted(RuntimeError):
    """The provider's limit needs a long wait (or is a daily cap): stop now
    with a message the student can act on instead of retrying for minutes."""


def _is_rate_limit(error: Exception) -> bool:
    return getattr(error, "status_code", None) == 429 or type(error).__name__ == "RateLimitError"


def _is_transient(error: Exception) -> bool:
    """A server error or dropped connection worth one quick retry (not a timeout:
    a slow model would just time out again)."""
    name = type(error).__name__
    if name in ("APITimeoutError", "TimeoutError"):
        return False
    status = getattr(error, "status_code", None)
    return (isinstance(status, int) and status >= 500) or name == "APIConnectionError"


def _retry_after(error: Exception) -> float | None:
    """Seconds the provider asked us to wait ("try again in 7m5.9s"), if stated."""
    m = _RETRY_IN_RE.search(str(error))
    if not m:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    parts = _DURATION_RE.findall(m.group(1))
    return sum(float(n) * scale[u] for n, u in parts) if parts else None


def _human_wait(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 90:
        return f"{round(seconds / 60)} minutes"
    return f"{max(1, round(seconds))} seconds"


def _rate_limit_message(error: Exception, model: str, wait: float | None) -> str:
    when = f" Try again in about {_human_wait(wait)}," if wait else " Try again later,"
    if _DAILY_RE.search(str(error)):
        what = f"Groq's daily token limit for {model} is used up."
    else:
        what = f"Groq's rate limit for {model} was reached."
    return what + when + " or pick a different model in the model picker."


def _create(client, *, cancelled=None, on_wait=None, **kwargs):
    """chat.completions.create that waits out short per-minute rate limits.

    Free Groq tiers cap tokens per minute, and a card-finding run makes many
    calls in a row. On a 429 asking for a short wait ("try again in 1.1s")
    we wait and resend, telling `on_wait` so the UI can show it. A daily
    cap or a long wait raises RateLimitExhausted at once instead.
    """
    transient_left = TRANSIENT_RETRIES
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as error:
            if not _is_rate_limit(error):
                if transient_left and _is_transient(error):
                    transient_left -= 1
                    _sleep(1.0, cancelled)
                    continue
                raise
            wait = _retry_after(error)
            # A daily cap is a rolling window, so a short stated wait is still
            # worth sitting through; an unknown wait on a daily cap is not.
            daily_unknown = wait is None and _DAILY_RE.search(str(error))
            if (daily_unknown or (wait is not None and wait > RATE_LIMIT_MAX_WAIT)
                    or attempt >= RATE_LIMIT_RETRIES):
                raise RateLimitExhausted(
                    _rate_limit_message(error, str(kwargs.get("model") or "this model"), wait)
                ) from error
            attempt += 1
            wait = (wait + 0.5) if wait is not None else 2.0 * attempt
            logging.getLogger(__name__).info("Rate limited; retrying in %.1fs", wait)
            if on_wait:
                on_wait(wait)
            _sleep(wait, cancelled)


def _sleep(seconds: float, cancelled=None) -> None:
    if cancelled is None:
        time.sleep(seconds)
    elif cancelled.wait(seconds):
        raise TutorCancelled()


def _wait_step(seconds: float) -> dict:
    return {"type": "tool",
            "text": f"Groq rate limit reached; waiting {_human_wait(seconds)} before continuing…"}


def _chat_completion(client, *, emit=None, cancelled=None, iteration=1, **kwargs):
    """Consume provider deltas while assembling the normal tool-loop response."""
    if emit is None:
        return _create(client, cancelled=cancelled, **kwargs)
    _check_cancelled(cancelled)
    emit({"event": "round", "iteration": iteration})
    stream = _create(client, cancelled=cancelled, stream=True,
                     on_wait=lambda s: emit({"event": "tool", "step": _wait_step(s)}), **kwargs)
    content, reasoning, tools = [], [], {}
    finish_reason = None
    try:
        for chunk in stream:
            _check_cancelled(cancelled)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            thought = getattr(delta, "reasoning", None)
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
                emit({"event": "reasoning_delta", "text": thought, "iteration": iteration})
            text = getattr(delta, "content", None)
            if isinstance(text, str) and text:
                content.append(text)
                if not tools:
                    emit({"event": "answer_delta", "text": text, "iteration": iteration})
            for part in getattr(delta, "tool_calls", None) or []:
                if not tools:
                    # Any text before a tool call is intermediate commentary.
                    emit({"event": "answer_reset"})
                tool = tools.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
                tool["id"] += getattr(part, "id", None) or ""
                function = getattr(part, "function", None)
                if function:
                    tool["name"] += getattr(function, "name", None) or ""
                    tool["arguments"] += getattr(function, "arguments", None) or ""
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        if finish_reason is None:
            raise RuntimeError("Model stream ended before completion")
        if finish_reason == "length":
            raise RuntimeError("Model output exceeded its token limit")
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    message = SimpleNamespace(
        content="".join(content), reasoning="".join(reasoning),
        tool_calls=[SimpleNamespace(id=tool["id"], function=SimpleNamespace(
            name=tool["name"], arguments=tool["arguments"],
        )) for _, tool in sorted(tools.items())],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


def _record_reasoning(message, steps: list[dict], iteration: int) -> None:
    """Keep provider-returned reasoning as display data, outside chat history."""
    text = getattr(message, "reasoning", None)
    if isinstance(text, str) and text.strip():
        steps.append({"type": "reasoning", "text": text.strip(), "iteration": iteration})


def _normalize_citations(text: str) -> str:
    """Coerce Unicode citation bracket variants to ASCII [n]."""
    # Lenticular 【】, tortoise-shell 〔〕, and fullwidth square ［］ → [ ]
    return re.sub(r"[【〔［]\s*(\d+)\s*[】〕］]", r"[\1]", text)


# Invisible / look-alike characters the model sometimes emits that render as
# empty boxes ("tofu") or stray glyphs in the browser. Map them to plain
# ASCII (or drop them entirely).
_CHAR_FIXES = {
    " ": " ", " ": " ", " ": " ", " ": " ",  # nbsp variants
    "⁠": "", "﻿": "",                                   # word-joiner, BOM
    "​": "", "‌": "", "‍": "", "️": "",       # zero-width / VS16
    "‐": "-", "‑": "-", "‒": "-",                  # hyphen variants
    "⁺": "+", "⁻": "-",                                 # superscript +/-
}
_CHAR_TRANS = {ord(k): (v or None) for k, v in _CHAR_FIXES.items()}

# Emoji and geometric-shape blocks (blue squares, blocks, dingbats) that have
# no place in a Step-1 answer and show up as colored/empty boxes.
_SYMBOL_RE = re.compile(
    "[\U0001F000-\U0001FAFF"   # emoji & pictographs (incl. 🟦 U+1F7E6)
    "▀-▟"            # block elements
    "■-◿"            # geometric shapes (□ ◻ ▪ ● …)
    "⬀-⯿"            # misc symbols & arrows (⬛ ⬜ …)
    "☀-➿]"           # misc symbols & dingbats
)


def _sanitize_answer(text: str) -> str:
    """Clean an LLM answer for browser display: normalize citation brackets,
    fix stray Unicode (odd spaces, hyphens, superscripts), and drop emoji /
    box glyphs so nothing renders as an empty square."""
    text = _normalize_citations(text)
    text = text.translate(_CHAR_TRANS)
    text = _SYMBOL_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Candidate summary (shared by the agentic deck-search tool)
# ---------------------------------------------------------------------------

def _candidate_summary(note: dict, max_len: int = 260) -> str:
    front = (note.get("front") or "").strip()
    back = (note.get("back") or "").strip()
    text = front if not back else f"{front} :: {back}"
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    # Top-level tag namespaces only, deduped.
    tops = []
    seen = set()
    for t in note.get("tags") or []:
        head = t.split("::", 1)[0].lstrip("#")
        if head and head not in seen:
            seen.add(head)
            tops.append(head)
        if len(tops) >= 4:
            break
    tag_str = " | ".join(tops)
    return f"{text}   [{tag_str}]" if tag_str else text


# ---------------------------------------------------------------------------
# Agentic chat (multi-turn, tool-using)
# ---------------------------------------------------------------------------

AGENT_SYSTEM = (
    "You are an agentic medical tutor helping a US medical student study for "
    "USMLE Step 1. You have tools to explore an Anki flashcard deck (BM25 "
    "full-text).\n\n"
    "This is a multi-turn conversation. Earlier questions and your earlier "
    "answers are included as context. For a follow-up (e.g. 'why?', 'what "
    "about in children?', 'compare that to X'), use that context to "
    "interpret what the student means — but ALWAYS gather fresh evidence "
    "from the deck for the new question. Never answer a follow-up from "
    "memory alone.\n\n"
    "Your job: given the student's question, gather evidence from the deck "
    "and then produce a concise, well-cited answer.\n\n"
    "Use the tools liberally and repeatedly — a single search is rarely "
    "enough. Iterate:\n"
    "1. Start with `search_deck` on the most direct phrasing.\n"
    "2. Run several more `search_deck` calls from different angles — "
    "synonyms, the specific named diagnosis/drug/mechanism, the contrasting "
    "concept, complications — until the topic is well covered. Calling "
    "`search_deck` many times with different queries is expected and "
    "encouraged.\n"
    "3. Use `read_note` when a snippet is truncated or ambiguous in a way "
    "that materially affects your answer.\n"
    "4. Keep going until you have solid, specific evidence — usually several "
    "tool calls. Stop early only if the deck clearly has nothing more to "
    "add; once the answer is well-supported, don't pad with redundant "
    "calls.\n\n"
    "Termination: to finish, respond with NO tool calls — the message "
    "content of that response becomes the final answer.\n\n"
    "Final answer rules:\n"
    "- Plain prose or simple Markdown (short paragraphs or a brief bullet "
    "list). No JSON, no code fences, no preamble like 'Here is the answer:'.\n"
    "- Keep it simple and within USMLE Step 1 scope. The first time a term "
    "or abbreviation an MS1 may not know appears, define it briefly in "
    "parentheses right after it.\n"
    "- Ground every factual claim that comes from the deck with plain ASCII "
    "bracket citations to flashcard refs you actually observed in tool "
    "results, e.g. [2] or [1][3]. Do NOT use Unicode brackets (【】, 〔〕, "
    "etc). Do NOT invent refs you didn't see. Do NOT add a bibliography — "
    "citations are inline.\n"
    "- If the deck doesn't cover the question (or covers it only partially), "
    "first say so plainly in one sentence — e.g. 'The deck doesn't cover "
    "this directly, but here's the standard Step 1 answer:' — and then "
    "answer from your own medical knowledge. Keep those un-cited "
    "supplementary claims clearly separated from any cited claims, and only "
    "state things you are confident are standard USMLE Step 1 content. "
    "Never fabricate flashcard citations to dress up outside knowledge."
)


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_deck",
            "description": (
                "Search the Anki flashcard deck with a BM25 query. Returns "
                "numbered cards with a ref you can cite in your answer as [N]. "
                "Each card includes a short snippet of its front :: back and "
                "its top-level topic tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Plain keywords or a short phrase. Examples: "
                            "'metoprolol contraindication cocaine', "
                            "'Sheehan syndrome postpartum', "
                            "'Kussmaul respirations DKA'."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 15,
                        "description": (
                            "Max number of cards to return (default 6, up to "
                            "15). Ask for more when you're surveying a topic."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": (
                "Read the full front+back+tags of a single flashcard by "
                "its GUID. Use it whenever a snippet looks relevant but "
                "doesn't give you the whole card."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string", "description": "The note GUID exactly as returned by search_deck."},
                },
                "required": ["guid"],
            },
        },
    },
]


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 40:
            v = v[:37] + "…"
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


class AgentSession:
    """One agentic chat run. Holds the observed flashcards + trace.

    Cards are numbered the first time the agent sees them (via any tool) and
    keep the same `[N]` ref across later turns, so the model can cite them
    consistently even if the same card surfaces in multiple searches.
    """

    MAX_TOOL_RESULT_CHARS = 9000

    def __init__(
        self,
        question: str,
        *,
        history: Iterable[dict] = (),
        tags: Iterable[str] = (),
        exact_tags: Iterable[str] = (),
        deck: str = "",
        system_prompt: str = AGENT_SYSTEM,
        tools: list | None = None,
        turn_directive: str = "",
        model: str | None = None,
        require_deck_search: bool = False,
    ) -> None:
        self.question = question
        self.model = get_model(model)
        self.require_deck_search = require_deck_search
        # Applies to this run only. History is replayed from raw conversation
        # text, so these response instructions never leak into later turns.
        self.turn_directive = turn_directive.strip()
        # The system prompt and tool set are configurable so the same loop can
        # drive both the RAG chat and the Tutor (which uses a different prompt
        # and the same deck-only tools).
        self.system_prompt = system_prompt
        self.tool_spec = tools if tools is not None else AGENT_TOOLS
        # Prior conversation turns ({role, content}) for follow-up context.
        # Only plain user/assistant text is kept — no tool-call plumbing.
        self.history: list[dict] = [
            {"role": h["role"], "content": str(h["content"])}
            for h in history
            if isinstance(h, dict)
            and h.get("role") in ("user", "assistant")
            and isinstance(h.get("content"), str)
            and h.get("content").strip()
        ]
        self.tags = [t for t in tags if t]
        self.exact_tags = [t for t in exact_tags if t]
        self.deck = (deck or "").strip()
        self.sources: list[dict] = []            # ordered by first-seen
        self.guid_to_ref: dict[str, int] = {}
        self.trace: list[dict] = []
        # Card moves prepared by `propose_move`; shown to the student as
        # "Review & move" buttons. Nothing is moved without their confirm.
        self.proposals: list[dict] = []
        self._on_event = None
        self._cancelled = None

    # ---- source bookkeeping --------------------------------------------

    def _register(self, note: dict) -> int:
        """Assign a stable ref to this card; reuse if seen before."""
        g = note.get("guid")
        if not g:
            return 0
        existing = self.guid_to_ref.get(g)
        if existing is not None:
            return existing
        ref = len(self.sources) + 1
        self.guid_to_ref[g] = ref
        self.sources.append(dict(note))
        return ref

    # ---- tool implementations ------------------------------------------

    def _tool_search_deck(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "empty_query"}
        limit = int(args.get("limit") or 6)
        limit = max(1, min(limit, 15))
        results = search.fast_search(
            query,
            limit=limit,
            tags=self.tags,
            exact_tags=self.exact_tags,
            deck=self.deck,
        )
        cards = []
        for r in results:
            ref = self._register(r)
            cards.append({
                "ref": ref,
                "guid": r.get("guid"),
                "snippet": _candidate_summary(r, max_len=280),
            })
        return {"query": query, "count": len(cards), "cards": cards}

    def _tool_read_note(self, args: dict) -> dict:
        guid = str(args.get("guid") or "").strip()
        if not guid:
            return {"error": "missing_guid"}
        note = search.get_note(guid)
        if not note:
            return {"error": "note_not_found", "guid": guid}
        ref = self._register(note)
        front = (note.get("front") or "").strip()
        back = (note.get("back") or "").strip()
        if len(front) > 1200:
            front = front[:1197] + "…"
        if len(back) > 1800:
            back = back[:1797] + "…"
        tops: list[str] = []
        seen: set[str] = set()
        for t in note.get("tags") or []:
            head = t.split("::", 1)[0].lstrip("#")
            if head and head not in seen:
                seen.add(head)
                tops.append(head)
            if len(tops) >= 6:
                break
        return {
            "ref": ref,
            "guid": guid,
            "front": front,
            "back": back,
            "tags": tops,
        }

    def _tool_propose_move(self, args: dict) -> dict:
        from . import anki_connect, organize

        selection = str(args.get("selection") or "").strip()
        if not selection:
            return {"error": "missing_selection"}
        if not str(args.get("target_deck") or "").strip():
            return {"error": "missing_target_deck",
                    "message": "Ask the student which deck the cards should go to."}
        try:
            anki_connect.check()
            existing = anki_connect.deck_names()
        except anki_connect.AnkiConnectError as e:
            return {"error": "anki_unavailable", "message": str(e)}
        try:
            resolved = organize.resolve_target(args.get("target_deck"), existing, self.question)
        except ValueError as e:
            return {"error": "invalid_deck_name", "message": str(e)}

        results, plan = self._select_cards(selection, deck=self.deck)
        nids = sorted({int(r["nid"]) for r in results if r.get("nid")})
        out = {
            "selection": selection,
            "notes_matched": len(nids),
            "target_deck": resolved["target"],
            "target_exists": resolved["exists"],
            "suggestions": resolved["suggestions"],
            "search_used": plan.get("explanation") or {
                k: v for k, v in plan.items() if v and k != "explanation"},
            "sample": [_candidate_summary(r, max_len=160) for r in results[:5]],
        }
        if not nids:
            out["note"] = "No cards matched, so there is nothing to move."
            return out
        proposal = {
            "id": len(self.proposals) + 1,
            "action": "move",
            "selection": selection,
            "target": resolved["target"],
            "target_exists": resolved["exists"],
            "suggestions": resolved["suggestions"],
            "count": len(nids),
            "nids": nids,
        }
        self.proposals.append(proposal)
        out["note"] = ("Nothing has moved yet. The student reviews a preview and "
                       "confirms with the 'Review & move' button under your reply.")
        out["reply_instructions"] = (
            "Do not call more tools. Reply in 1-3 short sentences and nothing else: "
            f"say {len(nids):,} notes matched '{selection}', that they will go to "
            f"'{resolved['target']}'"
            + ("" if resolved["exists"] else " (a new deck will be created)")
            + ", and that they can click 'Review & move' below to preview and confirm"
            + (f"; mention these existing decks in case they meant one: {resolved['suggestions']}"
               if resolved["suggestions"] else "")
            + ". Do not explain medical content, do not describe the cards, no citations."
        )
        return out

    def _find_nids(self, description: str, context_deck: str = "") -> tuple[list[int], dict[int, dict], bool]:
        """Collection-wide card finder (the deck limit is applied in Anki, which
        is authoritative even if this app's index is out of date). Returns
        (nids, notes by nid, whether a tag subtree matched directly).
        `context_deck` only disambiguates which tag subtree is meant."""
        results, plan = self._select_cards(description, deck="", context_deck=context_deck)
        by_nid = {int(r["nid"]): r for r in results if r.get("nid")}
        return sorted(by_nid), by_nid, bool(plan.get("tag_subtrees"))

    def _select_cards(self, selection: str, *, deck: str, context_deck: str = "") -> tuple[list[dict], dict]:
        """Every card matching a plain-English selection, as (results, plan).

        A selection that names a resource and maps to a single tag subtree
        ("Boards and Beyond GI anatomy") is answered straight from the tag
        tree, with no model calls. Anything else goes to the AI card finder.
        """
        from . import organize

        direct = direct_tag_selection(selection, deck=deck, context_deck=context_deck)
        if direct is not None:
            results, tags = direct
            if self._on_event:
                self._on_event({"event": "tool", "step": {"type": "tool", "text": (
                    f"Matched tag {', '.join(tags)} directly: {len(results):,} notes.")}})
            return results, {"tag_subtrees": tags,
                             "explanation": "Matched the named resource's tag directly."}

        def relay(event):
            # Surface the card finder's tag browsing in Thinking; its token-level
            # reasoning stays internal so it can't interleave with the agent's.
            if self._on_event and event.get("event") == "tool":
                self._on_event({"event": "tool", "step": {**event["step"], "type": "tool"}})

        results, plan, debug = assisted_search(
            selection, limit=organize.MAX_NOTES, deck=deck, max_iterations=10,
            model=self.model, on_event=relay if self._on_event else None,
            cancelled=self._cancelled,
        )
        if debug.get("rate_limited"):
            raise RateLimitExhausted(debug["rate_limited"])
        return results, plan

    def _tool_propose_card_action(self, args: dict) -> dict:
        from . import anki_connect, organize

        action = str(args.get("action") or "").strip().lower()
        if action not in organize.CARD_ACTIONS:
            return {"error": "action must be 'suspend' or 'unsuspend'"}
        deck_raw = str(args.get("deck") or "").strip()
        selection = str(args.get("selection") or "").strip()
        except_selection = str(args.get("except_selection") or "").strip()
        if not deck_raw and not selection:
            return {"error": "missing_scope",
                    "message": "Ask the student which deck or which cards to change."}
        try:
            anki_connect.check()
            existing = anki_connect.deck_names()
        except anki_connect.AnkiConnectError as e:
            return {"error": "anki_unavailable", "message": str(e)}
        deck = ""
        if deck_raw:
            try:
                resolved = organize.resolve_target(deck_raw, existing, self.question)
            except ValueError as e:
                return {"error": "invalid_deck_name", "message": str(e)}
            if not resolved["exists"]:
                return {"error": "deck_not_found", "deck": resolved["target"],
                        "suggestions": resolved["suggestions"],
                        "message": "No deck by that name. Ask which deck they meant."}
            deck = resolved["target"]

        nids = None
        if selection:
            nids, _, _ = self._find_nids(selection, context_deck=deck)
            if nids and deck:
                # Only the deck's own notes matter, and a collection-wide match
                # (e.g. every Boards and Beyond note) can exceed MAX_NOTES.
                nids = organize.notes_in_deck(deck, nids)
            if not nids:
                return {"error": "no_matching_cards", "selection": selection,
                        "message": "No cards matched that description"
                                   + (f" in '{deck}'" if deck else "") + ", so nothing would change."}
            if len(nids) > organize.MAX_NOTES:
                return {"error": "too_many_cards", "message": (
                    f"That's more than {organize.MAX_NOTES:,} notes. Ask the student to name a deck "
                    "or narrow the description.")}
        exclude: list[int] = []
        kept_sample: list[str] = []
        if except_selection:
            exclude, by_nid, by_tag = self._find_nids(except_selection, context_deck=deck)
            # Cards to KEEP favour recall: missing one would suspend a card the
            # student wanted. So for a short topic ("GERD") add every card whose
            # text contains those words. Not when a tag subtree matched (the
            # tag is exact), and not for long descriptions: every word must
            # match, and words like "boards", "section", "and" are on
            # thousands of cards, which kept 1,068 extra cards in one test.
            if not by_tag and _is_short_topic(except_selection):
                for r in search.fast_search("", limit=organize.MAX_NOTES, any_keywords=[except_selection]):
                    if r.get("nid"):
                        by_nid.setdefault(int(r["nid"]), r)
            exclude = sorted(by_nid)
            if not exclude:
                # Never fall back to changing everything when the exception
                # can't be found: that would suspend the cards they wanted kept.
                return {"error": "exception_not_found", "except_selection": except_selection,
                        "message": "Couldn't find the cards to keep, so nothing was prepared. "
                                   "Ask the student to describe them differently."}
            if deck:
                # Keep only notes that are in the deck: the rest can't be
                # suspended here anyway, and the full list can exceed MAX_NOTES.
                exclude = organize.notes_in_deck(deck, exclude)
                deck_has_cards = bool(exclude) or bool(
                    anki_connect.invoke("findCards", query=organize._search_term(deck)))
                if not exclude and deck_has_cards:
                    # None of the cards to keep are in this deck. Don't silently
                    # turn "everything except X" into "everything".
                    return {"error": "exception_not_in_deck", "except_selection": except_selection,
                            "message": f"None of the cards about '{except_selection}' are in "
                                       f"'{deck}', so nothing was prepared. Tell the student and "
                                       "ask whether they still want the whole deck changed."}
                kept_sample = [by_nid[n]["front"][:120] for n in exclude[:6] if n in by_nid]
            if len(exclude) > organize.MAX_KEEP_NOTES:
                return {"error": "too_many_cards", "message": (
                    f"The cards to keep are more than {organize.MAX_KEEP_NOTES:,} notes. Ask the "
                    "student to name a deck or narrow the description.")}
        spec = {"action": action, "deck": deck, "nids": nids, "exclude_nids": exclude}
        preview = organize.preview_card_action(spec)
        out = {
            "action": action, "deck": deck, "selection": selection,
            "except_selection": except_selection,
            "cards_to_change": preview["cards_to_change"],
            "already": preview["already"], "kept_cards": preview["kept"],
            "kept_sample": kept_sample,
        }
        if not preview["cards_to_change"]:
            if preview["already"] or preview["kept"]:
                out["note"] = f"Nothing to do: those cards are already {action}ed."
            else:
                where = f"The deck '{deck}'" if deck else "That selection"
                out["note"] = (f"Nothing to do: {where} has no cards right now (it may be empty "
                               "or the cards were moved elsewhere). Tell the student plainly.")
            return out
        self.proposals.append({
            "id": len(self.proposals) + 1, "action": action, "deck": deck,
            "selection": selection, "except_selection": except_selection,
            "nids": nids, "exclude_nids": exclude,
            "count": preview["cards_to_change"], "kept": preview["kept"],
            "kept_sample": kept_sample, "target": deck,
        })
        verb = "suspended" if action == "suspend" else "unsuspended"
        out["reply_instructions"] = (
            "Do not call more tools. Reply in 1-3 short sentences and nothing else: "
            f"say {preview['cards_to_change']:,} cards"
            + (f" in '{deck}'" if deck else "") + f" will be {verb}"
            + (f", keeping {preview['kept']:,} cards about '{except_selection}'" if except_selection else "")
            + f", and that they can click 'Review & {action}' below to preview and confirm. "
            "Nothing has changed yet. Do not explain medical content; no citations."
        )
        return out

    def _tool_propose_undo(self, args: dict) -> dict:
        from . import organize

        raw = args.get("change_ids")
        wanted = [str(c).strip() for c in raw] if isinstance(raw, list) else []
        wanted = [c for c in dict.fromkeys(wanted) if c]
        if not wanted:
            return {"error": "missing_change_ids",
                    "message": "Pick the change ids to undo from the recent changes list."}
        recent = {m["id"]: m for m in organize.list_moves(limit=organize.HISTORY_LIMIT)}
        unknown = [c for c in wanted if c not in recent]
        if unknown:
            return {"error": "unknown_change_ids", "ids": unknown,
                    "message": "Those ids aren't in the change history. Use ids from the list."}
        already = [c for c in wanted if recent[c].get("status") == "undone"]
        todo = [recent[c] for c in wanted if recent[c].get("status") != "undone"]
        if not todo:
            return {"note": "Those changes were already undone; nothing to do. Tell the student.",
                    "already_undone": already}
        todo.sort(key=lambda m: m.get("created") or 0, reverse=True)      # newest first
        changes = [{k: m.get(k) for k in ("id", "action", "cards", "target", "label", "created")}
                   for m in todo]
        self.proposals.append({
            "id": len(self.proposals) + 1, "action": "undo", "changes": changes,
            "count": sum(int(m.get("cards") or 0) for m in todo),
        })
        described = "; ".join(
            f"{_describe_change(m)}" for m in todo)
        return {
            "changes_to_undo": len(todo), "already_undone": already,
            "reply_instructions": (
                "Do not call more tools. Reply in 1-3 short sentences and nothing else: "
                f"say these will be undone: {described}; and that they can click "
                "'Review & undo' below to confirm. Nothing has changed yet."
                + (" Mention that some of the changes were already undone." if already else "")
            ),
        }

    def _dispatch(self, name: str, args: dict) -> dict:
        try:
            if name == "search_deck":
                return self._tool_search_deck(args)
            if name == "read_note":
                return self._tool_read_note(args)
            offered = {t["function"]["name"] for t in self.tool_spec}
            if name == "propose_move" and name in offered:
                return self._tool_propose_move(args)
            if name == "propose_card_action" and name in offered:
                return self._tool_propose_card_action(args)
            if name == "propose_undo" and name in offered:
                return self._tool_propose_undo(args)
            return {"error": f"unknown_tool:{name}"}
        except (TutorCancelled, RateLimitExhausted):
            raise
        except Exception as e:
            return {"error": f"tool_exception:{type(e).__name__}:{e}"}

    def _summarize(self, name: str, args: dict, result: dict) -> str:
        if "error" in result:
            return f"{name}({_fmt_args(args)}) → error: {result['error']}"
        if name == "search_deck":
            return (
                f"search_deck(q={args.get('query', '')!r}) → "
                f"{result.get('count', 0)} cards"
            )
        if name == "read_note":
            return (
                f"read_note({str(args.get('guid', ''))[:10]}…) → "
                f"ref [{result.get('ref', '?')}]"
            )
        if name == "propose_card_action":
            kept = f", keeping {result.get('kept_cards', 0)} cards" if result.get("except_selection") else ""
            return (f"Prepared {result.get('action')}: {result.get('cards_to_change', 0)} cards "
                    f"in {result.get('deck') or 'the matched notes'}{kept}")
        if name == "propose_move":
            new = "" if result.get("target_exists") else " (new deck)"
            return (
                f"Prepared move: {result.get('notes_matched', 0)} notes "
                f"({result.get('selection', '')!r}) → {result.get('target_deck', '')}{new}"
            )
        return f"{name}({_fmt_args(args)})"

    def _tool_choice(self, iteration: int):
        """The first round must use the deck, so answers stay grounded.

        With only search tools that means forcing `search_deck`. When the
        session can also prepare card moves, any tool is allowed in the first
        round: an organizing request should go straight to `propose_move`,
        and the provider rejects a call to any tool other than a forced one.
        """
        if not (self.require_deck_search and iteration == 0):
            return "auto"
        if any(t["function"]["name"] in PROPOSAL_TOOLS for t in self.tool_spec):
            return "required"
        return {"type": "function", "function": {"name": "search_deck"}}

    # ---- main loop -----------------------------------------------------

    def run(self, *, max_iterations: int = 6, on_event=None, cancelled=None) -> tuple[str, list[dict], dict]:
        debug: dict = {
            "model": self.model,
            "agent_trace": self.trace,
            "iterations": 0,
            "thinking": [],
        }
        t0 = time.perf_counter()
        self._on_event, self._cancelled = on_event, cancelled
        debug["move_proposals"] = self.proposals

        if not self.question.strip():
            return "Ask a question.", [], debug

        client = get_client()
        system_prompt = self.system_prompt
        if self.turn_directive:
            system_prompt += f"\n\nInstructions for this reply only:\n{self.turn_directive}"
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if self.tags or self.exact_tags:
            scope_bits = []
            if self.exact_tags:
                scope_bits.append(
                    "exact tag filter(s) active: " + ", ".join(self.exact_tags)
                )
            if self.tags:
                scope_bits.append(
                    "tag substring filter(s) active: " + ", ".join(self.tags)
                )
            messages.append({
                "role": "user",
                "content": (
                    "Scope: deck searches are pre-filtered to these tags, so "
                    "you don't need to reference them in queries. "
                    + "; ".join(scope_bits)
                ),
            })
        # Replay prior conversation turns so follow-ups have context. These
        # are plain text only — the tool-call loop for THIS turn starts fresh.
        for h in self.history:
            messages.append({"role": h["role"], "content": h["content"]})
        # Send the student's entire message verbatim. Mixing app instructions
        # into the pasted card can make a model misinterpret its boundaries.
        messages.append({"role": "user", "content": self.question})

        final_answer = ""
        hit_limit = False

        def complete(i: int, choice):
            return _chat_completion(
                client, emit=on_event, cancelled=cancelled, iteration=i + 1,
                model=self.model,
                messages=messages,
                tools=self.tool_spec,
                tool_choice=choice,
                temperature=0.2,
                max_completion_tokens=1500,
                timeout=RERANK_TIMEOUT,
                **reasoning_kwargs(self.model),
                **reasoning_output_kwargs(self.model),
            )

        for i in range(max_iterations):
            t_iter = time.perf_counter()
            choice = self._tool_choice(i)
            try:
                try:
                    resp = complete(i, choice)
                except TutorCancelled:
                    raise
                except Exception as e:
                    # "required" lets the model pick any tool, but for a message
                    # no tool fits (a greeting, an unsupported request) it may
                    # answer in text, which the provider rejects. Retry that
                    # round once with tools optional instead of failing.
                    if choice != "required" or "did not call a tool" not in str(e):
                        raise
                    debug["tool_choice_fallback"] = True
                    resp = complete(i, "auto")
            except (TutorCancelled, RateLimitExhausted):
                raise
            except Exception as e:
                debug["error"] = f"{type(e).__name__}:{e}"
                break

            msg = resp.choices[0].message
            _record_reasoning(msg, debug["thinking"], i + 1)
            debug["finish_reason"] = getattr(resp.choices[0], "finish_reason", None)
            debug["iterations"] = i + 1
            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                final_answer = (msg.content or "").strip()
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                _check_cancelled(cancelled)
                name = tc.function.name or ""
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch(name, args)
                self.trace.append({
                    "iter": i + 1,
                    "tool": name,
                    "args": args,
                    "summary": self._summarize(name, args, result),
                    "ms": round((time.perf_counter() - t_iter) * 1000, 1),
                })
                debug["thinking"].append({
                    "type": "tool", "text": self.trace[-1]["summary"], "iteration": i + 1,
                })
                if on_event:
                    on_event({"event": "tool", "step": debug["thinking"][-1]})
                payload = json.dumps(result, default=str)
                if len(payload) > self.MAX_TOOL_RESULT_CHARS:
                    payload = payload[: self.MAX_TOOL_RESULT_CHARS - 20] + '..."TRUNCATED"}'
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": payload,
                })
        else:
            hit_limit = True

        if hit_limit and not final_answer:
            # Force a final answer without further tools.
            messages.append({
                "role": "user",
                "content": (
                    "You've reached the tool-call budget. Produce your final "
                    "answer now using only the refs you've already observed. "
                    "Do not call any more tools."
                ),
            })
            try:
                resp = _chat_completion(
                    client, emit=on_event, cancelled=cancelled, iteration=max_iterations + 1,
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    max_completion_tokens=1200,
                    timeout=RERANK_TIMEOUT,
                    **reasoning_kwargs(self.model),
                    **reasoning_output_kwargs(self.model),
                )
                _record_reasoning(resp.choices[0].message, debug["thinking"], max_iterations + 1)
                final_answer = (resp.choices[0].message.content or "").strip()
                debug["finish_reason"] = getattr(resp.choices[0], "finish_reason", None)
                debug["forced_finalize"] = True
            except (TutorCancelled, RateLimitExhausted):
                raise
            except Exception as e:
                debug["finalize_error"] = f"{type(e).__name__}:{e}"

        answer = _sanitize_answer(final_answer)

        cited: set[int] = set()
        for m in re.finditer(r"\[(\d+)\]", answer):
            cited.add(int(m.group(1)))

        labeled_sources: list[dict] = []
        for note in self.sources:
            ref = self.guid_to_ref.get(note.get("guid", ""), 0)
            if not ref:
                continue
            labeled = dict(note)
            labeled["ref"] = str(ref)
            labeled["cited"] = ref in cited
            labeled_sources.append(labeled)
        # Cited cards first, then the rest in first-seen order.
        labeled_sources.sort(key=lambda s: (not s["cited"], int(s["ref"])))

        debug["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        debug["tool_calls"] = len(self.trace)
        debug["observed_sources"] = len(self.sources)
        debug["cited"] = sorted(cited)

        if not answer:
            debug.setdefault("error", debug.get("finalize_error") or "empty_response")

        return answer, labeled_sources, debug


# ---------------------------------------------------------------------------
# Tutor — agentic, deck-grounded explanations. Multi-turn conversation.
# ---------------------------------------------------------------------------

PROPOSE_MOVE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_move",
        "description": (
            "Prepare a move of a set of cards into another Anki deck, for when "
            "the student asks to move / put / add / organize cards into a deck. "
            "Finds every matching card (it browses the real tag tree itself) "
            "and returns how many notes matched, the resolved destination, and "
            "sample cards. It does NOT move anything: the student reviews a "
            "preview and confirms with a button shown under your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selection": {
                    "type": "string",
                    "description": (
                        "Plain-English description of the cards, keeping every "
                        "resource, system, and filter the student named, e.g. "
                        "'Boards and Beyond GI cards'. Do not include the destination."
                    ),
                },
                "target_deck": {
                    "type": "string",
                    "description": (
                        "Destination deck copied EXACTLY as the student wrote "
                        "it, every word included (deck names can contain words "
                        "like 'cards'), with '::' between levels if they gave a "
                        "path. E.g. 'GI', 'AnKing Step Deck::GI cards'. The app "
                        "matches it to their existing decks."
                    ),
                },
            },
            "required": ["selection", "target_deck"],
        },
    },
}

# The agent drives its own retrieval with the deck tools, and can prepare
# (never perform) card moves.
TUTOR_TOOLS = [
    t for t in AGENT_TOOLS
    if t["function"]["name"] in ("search_deck", "read_note")
]

PROPOSE_CARD_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_card_action",
        "description": (
            "Prepare suspending or unsuspending cards, e.g. 'in my GI deck, "
            "suspend everything except GERD' -> action='suspend', "
            "deck='GI', except_selection='GERD'. Finds the cards itself and "
            "returns counts. It does NOT change anything: the student reviews "
            "a preview and confirms with a button under your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["suspend", "unsuspend"]},
                "deck": {
                    "type": "string",
                    "description": (
                        "Deck to act within, copied EXACTLY as the student wrote "
                        "it (subdecks included). Omit only if they named no deck."
                    ),
                },
                "selection": {
                    "type": "string",
                    "description": (
                        "Which cards to act on, in plain English. Omit to act on "
                        "every card in `deck`."
                    ),
                },
                "except_selection": {
                    "type": "string",
                    "description": (
                        "Cards to leave alone, e.g. 'GERD' for 'everything "
                        "except GERD'. Keep the student's topic wording."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}

PROPOSE_UNDO_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_undo",
        "description": (
            "Prepare undoing earlier changes this app made (card moves, "
            "suspends, unsuspends), e.g. 'undo that', 'put the GI cards back', "
            "'undo everything I did to my GI subdeck'. Pick the ids from the "
            "RECENT CHANGES list in your instructions. It does NOT undo "
            "anything: the student confirms with a button under your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "change_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids of the changes to undo, from RECENT CHANGES.",
                },
            },
            "required": ["change_ids"],
        },
    },
}

PROPOSAL_TOOLS = ("propose_move", "propose_card_action", "propose_undo")
RECENT_CHANGES_SHOWN = 10


def _describe_change(m: dict) -> str:
    cards = f"{int(m.get('cards') or 0):,} cards"
    action = m.get("action") or "move"
    what = (f"{cards} {action}ed" + (f" in '{m['target']}'" if m.get("target") else "")
            if action in ("suspend", "unsuspend") else f"{cards} moved to '{m.get('target')}'")
    return what + (f" (request: \"{m['label']}\")" if m.get("label") else "")


def _recent_changes_prompt() -> str:
    """The change history, so 'undo that' can name exact ids."""
    from . import organize
    try:
        moves = organize.list_moves(limit=RECENT_CHANGES_SHOWN)
    except Exception:
        return ""
    if not moves:
        return "\n\nRECENT CHANGES: none yet, so there is nothing to undo."
    now = time.time()
    lines = []
    for m in moves:
        mins = max(0, int((now - (m.get("created") or now)) // 60))
        age = f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"
        status = "ALREADY UNDONE" if m.get("status") == "undone" else "active"
        lines.append(f"- id {m['id']} · {age} · {status} · {_describe_change(m)}")
    return ("\n\nRECENT CHANGES (newest first; only 'active' ones can be undone):\n"
            + "\n".join(lines))

# ---------------------------------------------------------------------------
# Anki Agent tab: organizes the collection (move / suspend / unsuspend).
# Separate from the Tutor, which stays read-only. The model can only PREPARE
# changes; each one is applied by the student's confirm click in the UI.
# ---------------------------------------------------------------------------

AGENT_ORGANIZER_TOOLS = TUTOR_TOOLS + [PROPOSE_MOVE_TOOL, PROPOSE_CARD_ACTION_TOOL, PROPOSE_UNDO_TOOL]

AGENT_ORGANIZER_SYSTEM = (
    "You are Anki Agent, an assistant that organizes a medical student's "
    "Anki collection. You can prepare three kinds of changes, which the "
    "student reviews in a preview and confirms with a button under your "
    "reply. You never change anything yourself.\n\n"
    "MOVING: If the student asks to move, put, add, or organize cards into "
    "a deck (e.g. 'put all Boards and Beyond GI cards in my GI deck'), call "
    "`propose_move` once with the card description and the destination "
    "copied exactly as they wrote it. If they didn't say where the cards "
    "should go, ask instead of guessing.\n\n"
    "SUSPENDING: If the student asks to suspend or unsuspend cards (e.g. 'in "
    "my GI deck, suspend everything except GERD'), call `propose_card_action` "
    "once.\n\n"
    "UNDOING: If the student asks to undo, revert, or put back an earlier "
    "change ('undo that', 'undo all the cards I just added to my GI "
    "subdeck'), call `propose_undo` once with the matching ids from RECENT "
    "CHANGES below. 'That' / 'the last change' means the newest active one; "
    "'everything' or a deck name means every active change matching it. If "
    "none match, or they are already undone, say so instead of calling it.\n\n"
    "After any of these tools, follow its `reply_instructions`: one to three short "
    "sentences, never claiming anything has changed yet. If a tool reports "
    "0 matches, a missing deck (mention its `suggestions`), or that Anki "
    "isn't reachable, say so plainly and suggest what to try. You may use "
    "`search_deck` or `read_note` to work out what the student means.\n\n"
    "Study questions belong in the Tutor tab: if asked one, say in one "
    "sentence that the Tutor tab answers questions with citations. If asked "
    "for a change you have no tool for (deleting cards, editing card text, "
    "changing deck options), say so in one sentence. No citations, no "
    "medical explanations."
)

TUTOR_AGENT_SYSTEM = (
    "You are a medical tutor helping a US MS1 study for USMLE Step 1. You "
    "have tools to search the student's own Anki flashcard deck "
    "(`search_deck`, BM25 full-text) and read a full card (`read_note`). "
    "Ground your explanation in their deck and cite the cards you use.\n\n"
    "This is a multi-turn conversation. The student usually pastes a "
    "flashcard or question as their message; later messages are follow-ups "
    "(a clarification, an expansion, or a new question). Use the earlier "
    "turns to interpret what they mean.\n\n"
    "A pasted flashcard may include its question, its answer, extra notes, "
    "and resource names across several paragraphs. Treat all of that as "
    "the supplied card. When a card is supplied without an explicit request, "
    "explain the fact it tests; the included answer is useful context. "
    "Do not ask the student to paste a card or question that is already "
    "present in their message.\n\n"
    "FOCUS: When the student supplies a card, center the explanation on "
    "the exact fact, cloze answer, or relationship that card tests. A brief, "
    "specific classification of the subject gives useful context before "
    "explaining that fact. Use "
    "the card's wording and clues to explain that point. Other retrieved "
    "cards support this explanation; they should not turn it into a "
    "general review of the surrounding topic. Add broader context only "
    "when it helps explain the card or the student asks for it.\n\n"
    "BEFORE answering, gather evidence from the deck — never answer from "
    "memory alone. You decide how to search and how much: there is no "
    "required number of calls and no required order. Start with the "
    "specific fact or relationship in the student's card or question. "
    "Search its direct phrasing, named disease/drug/mechanism, synonyms, "
    "and abbreviations as needed to find the relevant evidence. Expand "
    "to related concepts when they help explain this specific card or "
    "answer the question. When results are thin or off "
    "target, reword and search again rather than settling for a weak hit; "
    "when a snippet looks relevant but incomplete, `read_note` it. Raise "
    "`limit` when you need more relevant candidate cards. Err on the side of one "
    "more search — the only calls worth skipping are ones you already know "
    "the answer to.\n"
    "Termination: to finish, respond with NO tool calls — that message "
    "becomes your answer.\n\n"
    "ANSWERING: Answer the student's actual message directly and "
    "conversationally — no fixed template, no headers, no preamble. If a "
    "given turn asks for a specific shape, follow it for that reply only and "
    "go back to answering conversationally afterwards.\n\n"
    "CITATIONS: Ground every factual claim that comes from the deck with "
    "plain ASCII bracket citations to flashcard refs you actually observed "
    "in tool results, e.g. [2] or [1][3]. Do NOT use Unicode brackets. Do "
    "NOT invent refs you didn't see, and do NOT add a bibliography — "
    "citations are inline. If the deck doesn't cover the concept (or covers "
    "it only partially), say so plainly in one short clause and then explain "
    "from standard Step 1 knowledge, keeping those un-cited claims clearly "
    "separate; never fabricate a flashcard citation to dress up outside "
    "knowledge.\n\n"
    "STYLE: The student is an MS1. Explain as simply as possible — short "
    "words, short sentences, the least jargon you can get away with. The "
    "first time any term, abbreviation, or concept an MS1 may not know "
    "appears, immediately define it in plain language in parentheses right "
    "after it; when in doubt, define it. Stay within Step 1 scope and never "
    "go deeper than the boards require. Output nothing beyond the answer "
    "itself (no greeting, no closing remark, no meta-commentary about your "
    "searches)."
)

# Added to the system instructions for the FIRST turn only, so the reply follows
# the flashcard breakdown shape while every follow-up stays a normal answer.
# Later requests rebuild the system prompt without this directive.
TUTOR_FIRST_TURN_FORMAT = (
    "Format for this reply only: two or three short sections with a BLANK LINE "
    "between each (so they render as separate paragraphs). No headers, "
    "labels, bullets, or preamble. Keep it short: aim for 60-100 words total.\n"
    "1. Open with ONE short, factual, declarative sentence identifying the "
    "card's subject and its specific, useful class or category. For an "
    "organism, identify what kind of organism or parasite it is; for a "
    "drug, identify its drug class. Generic labels such as 'a parasite' "
    "or 'a drug' are too vague. Include a defining feature relevant to "
    "the card if helpful. This sentence should orient the student, not "
    "simply repeat the card's answer or explain its mnemonic. "
    "If the student supplied a question instead, answer it directly.\n"
    "2. Explain WHY that exact fact or answer is true in plain language, "
    "connecting the relevant mechanism or reasoning back to the card's "
    "wording and making the cloze answer explicit when present. Stay at "
    "USMLE Step 1 depth. Use one or two short sentences with only the "
    "background needed to understand this card.\n"
    "3. One sentence contrasting it with a similar, frequently-confused, "
    "high-yield Step 1 concept, stating the single feature in this card "
    "that tells them apart. Include this only if it directly helps the "
    "student understand or remember this card; otherwise omit it."
)

TUTOR_TIMEOUT = 60.0
TUTOR_MAX_HISTORY = 40   # user+assistant turns kept in context
TUTOR_MAX_RETRIES = 3   # One initial attempt, then at most three retries.


_MISSING_INPUT_RE = re.compile(
    r"\b(?:your|the|this)\s+(?:message|input|question|card|text)\s+"
    r"(?:(?:is|was|seems|looks|appears)(?:\s+(?:to be|like|completely|still)){0,3}"
    r"|(?:came|arrived)(?:\s+(?:through|in))?|got)\s+(?:empty|blank|cut off)\b"
    r"|\bno\s+(?:card|question|message|text|input)\b"
    r"[^.!?\n]{0,45}\b(?:attached|provided|included|received|supplied)\b"
    r"|\b(?:didn't|haven't|have not|did not|don't|do not)\s+"
    r"(?:receive|include|provide|supply|attach|see)\b"
    r"[^.!?\n]{0,70}\b(?:card|question|message|text|input)\b",
    re.IGNORECASE,
)


def _claims_missing_input(answer: str) -> bool:
    """Recognize a false missing-message response, not an empty medical organ."""
    opening = answer[:600].replace("’", "'")
    return bool(_MISSING_INPUT_RE.search(opening))


def tutor_chat(
    messages: list[dict], *, deck: str = "", max_iterations: int = 12,
    model: str | None = None, on_event=None, cancelled=None,
) -> tuple[str, list[dict], dict]:
    """Agentic, deck-grounded tutor reply.

    `messages` is the full conversation as {role, content} dicts (role in
    {'user','assistant'}); the last must be the current user turn.
    `max_iterations` counts model round-trips, not individual tool calls —
    the model can batch several searches into one round, so the practical
    search budget is higher than this number. Returns
    (answer, sources, debug): the reply text with inline [N] citations, the
    observed flashcards (cited-first, each with a string `ref` and a `cited`
    flag), and a debug dict including the agent trace.
    Failed responses get up to three retries, each with a fresh search session.
    The tutor is read-only: it has no tools that change the collection.
    """
    return _chat_with_retries(
        messages, system_prompt=TUTOR_AGENT_SYSTEM, tools=TUTOR_TOOLS,
        first_turn_format=TUTOR_FIRST_TURN_FORMAT, deck=deck,
        max_iterations=max_iterations, model=model, on_event=on_event, cancelled=cancelled,
    )


def agent_chat(
    messages: list[dict], *, deck: str = "", max_iterations: int = 12,
    model: str | None = None, on_event=None, cancelled=None,
) -> tuple[str, list[dict], dict]:
    """Anki Agent tab: prepares moves / suspends from plain-English requests.

    Same contract as `tutor_chat`; prepared changes are returned in
    `debug["move_proposals"]` and are applied only by the user's confirm click.
    """
    return _chat_with_retries(
        messages, system_prompt=AGENT_ORGANIZER_SYSTEM + _recent_changes_prompt(),
        tools=AGENT_ORGANIZER_TOOLS,
        first_turn_format="", deck=deck,
        max_iterations=max_iterations, model=model, on_event=on_event, cancelled=cancelled,
    )


def _chat_with_retries(
    messages: list[dict], *, system_prompt: str, tools: list, first_turn_format: str,
    deck: str = "", max_iterations: int = 12,
    model: str | None = None, on_event=None, cancelled=None,
) -> tuple[str, list[dict], dict]:
    """Shared multi-turn loop with retries for the Tutor and Anki Agent tabs."""
    base_prompt = system_prompt
    trimmed = (
        messages[-TUTOR_MAX_HISTORY:]
        if len(messages) > TUTOR_MAX_HISTORY
        else list(messages)
    )
    question = trimmed[-1]["content"] if trimmed else ""
    history = trimmed[:-1]
    t0 = time.perf_counter()
    failures: list[dict] = []
    thinking: list[dict] = []
    for attempt in range(TUTOR_MAX_RETRIES + 1):
        _check_cancelled(cancelled)
        if attempt:
            # Brief backoff avoids immediately repeating a transient failure.
            delay = 0.5 * 2 ** (attempt - 1)
            if cancelled is None:
                time.sleep(delay)
            elif cancelled.wait(delay):
                raise TutorCancelled()
        system_prompt = base_prompt
        if failures and failures[-1]["error"] == "false_empty_input":
            system_prompt += (
                "\n\nA previous attempt incorrectly said the supplied message "
                "was empty. The latest user message contains the full card or "
                "question. Read all its paragraphs and explain its medical "
                "content. Resource labels and a trailing card number are "
                "ancillary text. Search using the medical terms in that "
                "message; do not ask the student to send it again."
            )
        # A fresh session discards unrelated evidence and the invalid reply
        # from a rejected attempt. Keep the selected model and real history.
        session = AgentSession(
            question,
            history=history,
            deck=deck,
            system_prompt=system_prompt,
            tools=tools,
            turn_directive="" if history else first_turn_format,
            model=model,
            require_deck_search=True,
        )
        run_kwargs = {"max_iterations": max_iterations}
        stream_steps: list[dict] = []
        if on_event:
            def emit_event(event):
                if event["event"] == "reasoning_delta":
                    previous = stream_steps[-1] if stream_steps else None
                    if previous and previous["type"] == "reasoning" and previous["iteration"] == event["iteration"]:
                        previous["text"] += event["text"]
                    else:
                        stream_steps.append({"type": "reasoning", "text": event["text"], "iteration": event["iteration"]})
                elif event["event"] == "tool":
                    stream_steps.append(event["step"])
                on_event({**event, "attempt": attempt + 1})
            on_event({"event": "attempt", "attempt": attempt + 1, "model": session.model})
            run_kwargs.update(on_event=emit_event, cancelled=cancelled)
        try:
            answer, sources, debug = session.run(**run_kwargs)
        except RateLimitExhausted as e:
            # Retrying can't help until the provider's window resets.
            step = {"type": "error", "text": str(e), "attempt": attempt + 1}
            thinking.extend({**s, "attempt": attempt + 1} for s in stream_steps)
            thinking.append(step)
            if on_event:
                on_event({"event": "retry", "step": step, "attempt": attempt + 1})
            logging.getLogger(__name__).warning("Rate limited: model=%s %s", session.model, e)
            return "", [], {
                "model": session.model, "thinking": thinking, "response_retries": attempt,
                "error": "rate_limited", "user_error": str(e),
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        thinking.extend({**step, "attempt": attempt + 1} for step in (
            stream_steps if on_event else debug.get("thinking", [])
        ))
        debug["thinking"] = thinking
        debug["response_retries"] = attempt
        debug["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        error = debug.get("error") or debug.get("finalize_error")
        if question.strip() and not error:
            if not answer.strip():
                error = "empty_response"
            elif _claims_missing_input(answer):
                error = "false_empty_input"
        if not error:
            debug["retry_failures"] = failures
            return answer, sources, debug

        failures.append({
            "attempt": attempt + 1,
            "error": error,
            "finish_reason": debug.get("finish_reason"),
        })
        debug["error"] = error
        debug["retry_failures"] = failures
        reason = {
            "empty_response": "The model returned no answer.",
            "false_empty_input": "The model misread the supplied message as empty.",
        }.get(error, "The model request failed.")
        thinking.append({
            "type": "retry" if attempt < TUTOR_MAX_RETRIES else "error",
            "text": reason + (" Retrying…" if attempt < TUTOR_MAX_RETRIES else " All attempts failed."),
            "attempt": attempt + 1,
        })
        if on_event:
            on_event({"event": "retry", "step": thinking[-1], "attempt": attempt + 1})
        logging.getLogger(__name__).warning(
            "Tutor attempt failed: model=%s attempt=%d/%d error=%s finish_reason=%s",
            session.model, attempt + 1, TUTOR_MAX_RETRIES + 1,
            error, debug.get("finish_reason"),
        )

    # A failed answer and its sources must never enter conversation history,
    # and a move prepared by a failed attempt must not be offered.
    debug.pop("move_proposals", None)
    return "", [], debug


# ---------------------------------------------------------------------------
# AI-assisted search — turn a plain-English request into a tag/keyword plan
# ---------------------------------------------------------------------------

ASSIST_SYSTEM = (
    "You convert a medical student's plain-English request into a structured "
    "search over their Anki deck. The deck has tens of thousands of "
    "hierarchical tags (levels separated by '::'), e.g. "
    "'Microbiology::Viruses::RNA::(+)Sense::Poliovirus'. "
    "You do NOT know exact tag paths from memory — you MUST discover them "
    "with the `find_tags` tool before using them. Probe several substrings "
    "(resource names, system, topic, and any organizing concept the user "
    "names such as a yield level or exam scope); read back the real paths "
    "and counts, and call it as many times as needed.\n\n"
    "Resource names are usually ABBREVIATED in tag paths (e.g. Boards and "
    "Beyond -> '#B&B', First Aid -> '#FirstAid', Sketchy Micro -> "
    "'#SketchyMicro'), and systems may be numbered (e.g. '10_GI'). If a "
    "`find_tags` probe misses or returns odd matches, use `browse_tags` to "
    "walk the tag tree level by level from the top (prefix '') down to the "
    "resource and topic the user means, then use the exact path you found. "
    "Prefer the current, high-count tag tree over small legacy or "
    "'Original_Decks' branches.\n\n"
    "You ALSO have `search_deck`, a BM25 full-text keyword search over the "
    "card front/back (optionally narrowed by tag substrings) — the same engine "
    "the plain search box uses. Use it to probe the deck directly: check "
    "whether a set of keywords actually returns the right cards, see roughly "
    "how many match, and refine your terms. This is the right tool when the "
    "request is about content that words capture better than any single tag. "
    "The final `keywords` / `any_keywords` you return are executed as exactly "
    "this BM25 search, so lean on keywords whenever there's no clean tag for "
    "the concept. Use both tools freely, in any order, as many times as "
    "needed.\n\n"
    "When done, respond with NO tool call and ONLY a JSON object (no prose, "
    "no code fence):\n"
    '{\n'
    '  "keywords": "<free-text terms that must ALL appear, or empty>",\n'
    '  "any_keywords": ["<alternative term>", ...],\n'
    '  "tag_filters": ["<tag substring that must appear>", ...],\n'
    '  "any_tags": ["<alternative tag substring>", ...],\n'
    '  "explanation": "<one sentence describing the search you built>"\n'
    "}\n\n"
    "AND vs OR — this matters:\n"
    "- `keywords` are ANDed (every term must appear); `tag_filters` are "
    "ANDed (every substring must appear). Use these to NARROW.\n"
    "- `any_keywords` is an OR group: a card matches if it contains ANY one "
    "of them. `any_tags` is an OR group: a card matches if ANY one of the "
    "substrings appears in its tags. Use these whenever the user asks for "
    "alternatives — 'X or Y', 'either', 'any of', a list of topics, or "
    "synonyms that should all surface. For example 'cardiology or pulmonology "
    "cards' -> any_tags with the two verified topic substrings; 'beta "
    "blockers or calcium channel blockers' -> any_keywords or any_tags as "
    "appropriate.\n\n"
    "Rules: tag substrings are matched case-insensitively against a tag path "
    "— prefer a distinctive verified substring (e.g. "
    "'#SketchyMicro::03_Viruses::01_RNA_(+)_Sense') over vague words. If the "
    "user asks for an organizing facet (e.g. 'high yield', 'Step 1 only'), "
    "find the matching tag with find_tags and add its verified substring — do "
    "not volunteer facets the user didn't ask for. Put content concepts into "
    "keywords/any_keywords. Any field may be empty/omitted. Never invent a "
    "tag substring you did not confirm via find_tags or browse_tags.\n\n"
    "IMPORTANT: each tag_filters entry may match a DIFFERENT tag on the same "
    "card. So 'resource X about topic Y' must be ONE substring spanning both "
    "levels of a single tag path (e.g. '#B&B::10_GI'), never two separate "
    "filters like ['#B&B', 'GI'], which also catches cards with any B&B tag "
    "plus any unrelated GI tag. If the same resource+topic exists under "
    "several exams (e.g. '#AK_Step1_v12::#B&B::10_GI' and "
    "'#AK_Step2_v12::#B&B::06_GI'), list each full substring in any_tags."
)

ASSIST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browse_tags",
            "description": (
                "List the child levels directly under a tag path, with the "
                "number of notes beneath each. Start with prefix '' for the "
                "top level, then descend (e.g. '#AK_Step1_v12', then "
                "'#AK_Step1_v12::#B&B'). Use this when you can't guess a "
                "tag's exact name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Tag path to list children of; '' for the top level.",
                    },
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_tags",
            "description": (
                "Look up real deck tags containing a substring "
                "(case-insensitive). Returns up to `limit` tags with note "
                "counts, most-used first. Use this to discover exact tag "
                "paths before putting them in tag_filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "substring": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 40,
                        "description": "Max tags to return (default 20).",
                    },
                },
                "required": ["substring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_deck",
            "description": (
                "Run a BM25 full-text keyword search over the deck (card "
                "front/back), optionally narrowed to tag substrings. Returns "
                "the match count and a few sample card snippets. Use this to "
                "test whether keywords retrieve the right cards before "
                "finalizing your plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "BM25 keywords or a short phrase.",
                    },
                    "tag_filters": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tag substrings to AND with the query.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Max sample snippets to return (default 6).",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

ASSIST_TIMEOUT = 25.0


def _loose_json_obj(raw: str) -> dict:
    """Parse a JSON object, tolerating code fences / surrounding prose."""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _str_list(plan: dict, key: str) -> list[str]:
    return [str(t).strip() for t in (plan.get(key) or []) if str(t).strip()]


def _norm_plan(plan: dict) -> dict:
    """Normalize a raw model plan into the canonical, OR-aware shape used by
    both the executor and the UI's plan echo."""
    plan = plan if isinstance(plan, dict) else {}
    return {
        "keywords": str(plan.get("keywords") or "").strip(),
        "any_keywords": _str_list(plan, "any_keywords"),
        "tag_filters": _str_list(plan, "tag_filters"),
        "any_tags": _str_list(plan, "any_tags"),
        "explanation": str(plan.get("explanation") or "").strip(),
    }


def _run_assist_plan(plan: dict, *, limit: int, deck: str = "") -> list[dict]:
    """Execute a plan: ANDed keywords/tag_filters, ORed any_keywords/any_tags,
    optionally scoped to a deck subtree."""
    p = _norm_plan(plan)
    if not (p["keywords"] or p["any_keywords"] or p["tag_filters"]
            or p["any_tags"] or deck):
        return []
    return search.fast_search(
        p["keywords"],
        limit=limit,
        tags=p["tag_filters"],
        any_tags=p["any_tags"],
        any_keywords=p["any_keywords"],
        deck=deck,
    )


def _run_planner(
    system_prompt: str,
    user_content: str,
    debug: dict,
    *,
    max_iterations: int = 6,
    timeout: float = ASSIST_TIMEOUT,
    model: str | None = None, on_event=None, cancelled=None,
) -> dict:
    """Drive the find_tags tool loop and return the model's final JSON plan.

    Shared by AI-assisted search and Deep search's scout phase. Records
    `iterations` and each `find_tags` lookup into `debug`.
    """
    model = get_model(model)
    client = get_client()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    plan: dict = {}
    for i in range(max_iterations):
        _check_cancelled(cancelled)
        debug["iterations"] = i + 1
        try:
            resp = _search_completion(
                client, debug["thinking"], iteration=i + 1, phase="plan",
                on_event=on_event, cancelled=cancelled,
                model=model,
                messages=messages,
                tools=ASSIST_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_completion_tokens=1200,
                timeout=timeout,
                **reasoning_kwargs(model),
                **reasoning_output_kwargs(model),
            )
        except TutorCancelled:
            raise
        except RateLimitExhausted as e:
            # Callers that act on the plan (card moves) must not read an
            # empty plan as "no cards matched".
            debug["error"] = f"{type(e).__name__}:{e}"
            debug["rate_limited"] = str(e)
            break
        except Exception as e:
            debug["error"] = f"{type(e).__name__}:{e}"
            break

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            plan = _loose_json_obj(msg.content or "")
            break

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            _check_cancelled(cancelled)
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            if tc.function.name == "find_tags":
                hits = search.find_tags(
                    str(args.get("substring") or ""),
                    limit=int(args.get("limit") or 20),
                )
                debug.setdefault("tag_lookups", []).append({
                    "substring": args.get("substring"),
                    "n": len(hits),
                })
                result = {"matches": hits}
                summary = f"Looked up tags containing {args.get('substring')!r}: {len(hits)} matching tags."
            elif tc.function.name == "browse_tags":
                prefix = str(args.get("prefix") or "").strip().strip(":")
                children = search.tag_children(prefix, limit=40)
                debug.setdefault("tag_browses", []).append({"prefix": prefix, "n": len(children)})
                # Names only, to keep tokens down: full path = prefix::name.
                result = {"prefix": prefix,
                          "children": [[c["tag"].rsplit("::", 1)[-1], c["notes"]] for c in children],
                          "format": "[name, notes]; full tag path = prefix::name"}
                where = repr(prefix) if prefix.strip() else "the top level"
                summary = f"Browsed tags under {where}: {len(children)} branches."
            elif tc.function.name == "search_deck":
                probe_q = str(args.get("query") or "").strip()
                probe_tags = [
                    str(t).strip()
                    for t in (args.get("tag_filters") or [])
                    if str(t).strip()
                ]
                n_samples = max(1, min(int(args.get("limit") or 6), 10))
                PROBE_CAP = 200
                hits = (
                    search.fast_search(probe_q, limit=PROBE_CAP, tags=probe_tags)
                    if (probe_q or probe_tags)
                    else []
                )
                debug.setdefault("searches", []).append({
                    "query": probe_q,
                    "tags": probe_tags,
                    "count": len(hits),
                })
                result = {
                    "query": probe_q,
                    "count": len(hits),
                    "capped": len(hits) >= PROBE_CAP,
                    "sample": [
                        _candidate_summary(h, max_len=200)
                        for h in hits[:n_samples]
                    ],
                }
                summary = f"Searched {probe_q!r}: {'at least ' if len(hits) >= PROBE_CAP else ''}{len(hits)} matching cards."
            else:
                result = {"error": f"unknown_tool:{tc.function.name}"}
                summary = f"Unknown search tool: {tc.function.name}"
            _search_activity(debug, summary, on_event=on_event, phase="plan", iteration=i + 1)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    return plan if isinstance(plan, dict) else {}


def _search_activity(debug: dict, text: str, *, on_event=None, phase="plan", **context) -> None:
    step = {"type": "tool", "text": text, "phase": phase, **context}
    debug.setdefault("thinking", []).append(step)
    if on_event:
        on_event({"event": "tool", "step": step})


def _search_completion(client, thinking: list[dict], *, iteration=1, phase="plan",
                       batch=None, on_event=None, cancelled=None, **kwargs):
    """Capture search reasoning; the JSON search plan remains internal."""
    context = {"phase": phase, "iteration": iteration}
    if batch is not None:
        context["batch"] = batch
    if on_event:
        def emit(event):
            if event["event"] == "tool":           # e.g. a rate-limit wait notice
                thinking.append({**event["step"], **context})
                on_event({**event, "step": {**event["step"], **context}})
                return
            if event["event"] != "reasoning_delta":
                return
            previous = thinking[-1] if thinking else None
            if previous and previous["type"] == "reasoning" and all(previous.get(k) == v for k, v in context.items()):
                previous["text"] += event["text"]
            else:
                thinking.append({"type": "reasoning", "text": event["text"], **context})
            on_event({**event, **context})
        return _chat_completion(client, emit=emit, cancelled=cancelled, iteration=iteration, **kwargs)
    response = _create(client, cancelled=cancelled, **kwargs)
    captured = []
    _record_reasoning(response.choices[0].message, captured, iteration)
    thinking.extend({**step, **context} for step in captured)
    return response


# ---------------------------------------------------------------------------
# Direct tag selection — "Boards and Beyond GI anatomy" -> one tag subtree,
# with no model calls. Conservative: any doubt falls back to the AI finder.
# ---------------------------------------------------------------------------

# Spoken resource names -> the tag level AnKing-style decks use for them.
RESOURCE_ALIASES = {
    "#B&B": ("boards and beyond", "boards & beyond", "boards n beyond", "b&b", "bnb", "b and b"),
    "#Pathoma": ("pathoma",),
    "#FirstAid": ("first aid", "firstaid"),
    "#Bootcamp": ("bootcamp", "boot camp"),
    "#Physeo": ("physeo",),
    "#Pixorize": ("pixorize",),
    "#Costanzo": ("costanzo",),
    "#NinjaNerd": ("ninja nerd", "ninjanerd"),
    "#Osmosis": ("osmosis",),
    "#SketchyMicro": ("sketchy micro", "sketchymicro"),
    "#SketchyPharm": ("sketchy pharm", "sketchy pharmacology", "sketchypharm"),
    "#SketchyPath": ("sketchy path", "sketchy pathology", "sketchypath"),
}
# Words that say "which cards" rather than naming a topic.
_FILLER_WORDS = frozenset("""
    a all an and any are cards card deck decks every everything for from in
    is its lecture lectures my notes note of on only section sections that
    the their them these this those to video videos with subdeck subdecks
    tagged tag tags
""".split())
# Topic words students use that differ from the tag spelling.
_WORD_ALIASES = {
    "gi": {"gi", "gastrointestinal", "gastro"},
    "gastrointestinal": {"gi", "gastrointestinal", "gastro"},
    "gastro": {"gi", "gastrointestinal", "gastro"},
    "cardiology": {"cardio", "cardiology", "cardiovascular"},
    "cardiovascular": {"cardio", "cardiology", "cardiovascular"},
    "cardio": {"cardio", "cardiology", "cardiovascular"},
    "pulmonary": {"pulm", "pulmonary", "pulmonology"},
    "pulm": {"pulm", "pulmonary", "pulmonology"},
    "neurology": {"neuro", "neurology"},
    "neuro": {"neuro", "neurology"},
    "heme": {"heme", "hematology"},
    "hematology": {"heme", "hematology"},
    "endo": {"endo", "endocrine", "endocrinology"},
    "endocrine": {"endo", "endocrine", "endocrinology"},
    "renal": {"renal", "kidney"},
    "repro": {"repro", "reproductive", "reproduction"},
    "reproductive": {"repro", "reproductive", "reproduction"},
    "embryo": {"embryo", "embryology"},
    "physio": {"physio", "physiology"},
    "physiology": {"physio", "physiology"},
    "pharm": {"pharm", "pharmacology"},
    "pharmacology": {"pharm", "pharmacology"},
    "micro": {"micro", "microbiology"},
    "path": {"path", "pathology"},
    "biochem": {"biochem", "biochemistry"},
    "psych": {"psych", "psychiatry"},
    "msk": {"msk", "musculoskeletal"},
}
_DIRECT_MAX_MATCHES = 3
_DIRECT_DOMINANT_SHARE = 0.9   # in a deck, a subtree with this share of matches wins a tie


def _words(text: str) -> list[str]:
    text = text.casefold().replace("step 1", "step1").replace("step 2", "step2")
    return [w.lstrip("0") or "0" if w.isdigit() else w
            for w in re.split(r"[^a-z0-9]+", text) if w]


def _word_in_segment(word: str, segment_words: list[str]) -> bool:
    wanted = _WORD_ALIASES.get(word, {word})
    for w in segment_words:
        if w in wanted or w.rstrip("s") == word.rstrip("s"):
            return True
        if len(word) >= 5 and w.startswith(word):          # "embryo" -> "embryology"
            return True
    return False


def _is_short_topic(text: str) -> bool:
    """A topic name ("GERD", "liver disease") rather than a description: at
    most 3 meaningful words and no resource name."""
    lowered = " " + " ".join(text.casefold().split()) + " "
    if any(f" {a} " in lowered for aliases in RESOURCE_ALIASES.values() for a in aliases):
        return False
    return 0 < len([w for w in _words(text) if w not in _FILLER_WORDS]) <= 3


def direct_tag_selection(selection: str, *, deck: str = "",
                         context_deck: str = "") -> tuple[list[dict], list[str]] | None:
    """(notes, tag subtrees) when `selection` names a resource and its other
    words pick out exactly one subtree of that resource's tags; else None.

    Every remaining topic word must match a tag level, and the shallowest
    matching subtree must be unique within each top-level deck tag (the same
    subtree in two decks, e.g. Step 1 and Step 2 Boards and Beyond, is fine).
    `deck` limits the notes returned; `context_deck` (default: `deck`) only
    breaks ties, keeping subtrees that have notes in that deck.
    """
    context_deck = context_deck or deck
    text = " " + " ".join(selection.casefold().split()) + " "
    resource = None
    for segment, aliases in RESOURCE_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if f" {alias} " in text:
                if resource and resource != segment:
                    return None                              # two resources named
                resource = segment
                text = text.replace(f" {alias} ", " ")
    if not resource:
        return None
    topic = [w for w in _words(text) if w not in _FILLER_WORDS]
    tags = search.tags_with_segment(resource)
    if not tags:
        return None

    matches: set[tuple[str, ...]] = set()
    for tag in tags:
        parts = tag.split("::")
        start = next(i for i, p in enumerate(parts) if p.casefold() == resource.casefold())
        for end in range(start + 1, len(parts) + 1):
            below = [_words(p) for p in parts[start + 1:end]]
            # Earlier levels (e.g. "#AK_Step1_v12") can satisfy "step1".
            above = [_words(p) for p in parts[:start]]
            if all(any(_word_in_segment(w, seg) for seg in below + above) for w in topic):
                matches.add(tuple(parts[:end]))
                break
    if matches and context_deck:
        # Within a deck, only subtrees that have notes there count, so "Boards
        # and Beyond anatomy" inside a GI deck means the GI anatomy section.
        present = search.tags_in_deck(context_deck)
        matches = {m for m in matches
                   if any(t == "::".join(m) or t.startswith("::".join(m) + "::") for t in present)}
    if not matches:
        return None
    # Keep the shallowest match per top-level tag; drop matches nested in others.
    shallow = min(len(m) for m in matches)
    best = [m for m in matches if len(m) == shallow]
    roots = [m[0] for m in best]
    if (len(set(roots)) != len(roots) or len(best) > _DIRECT_MAX_MATCHES) and context_deck:
        # Notes often carry a second system's tag, so "anatomy" in a GI deck
        # also touches a few Pulm / Renal anatomy notes. One subtree holding
        # nearly all of the deck's matching notes is the one that was meant.
        notes = {m: {n["guid"] for n in search.notes_under_tags(["::".join(m)], deck=context_deck)}
                 for m in best}
        top = max(best, key=lambda m: len(notes[m]))
        every = set().union(*notes.values())
        if notes[top] and len(notes[top]) >= _DIRECT_DOMINANT_SHARE * len(every):
            best, roots = [top], [top[0]]
    if len(set(roots)) != len(roots) or len(best) > _DIRECT_MAX_MATCHES:
        return None                                          # ambiguous
    prefixes = sorted("::".join(m) for m in best)
    notes = search.notes_under_tags(prefixes, deck=deck)
    return (notes, prefixes) if notes else None


def assisted_search(
    query: str, *, limit: int = 60, deck: str = "", max_iterations: int = 6,
    model: str | None = None, on_event=None, cancelled=None,
) -> tuple[list[dict], dict, dict]:
    """Plain-English -> deck search. Returns (results, plan, debug).

    The model uses the `find_tags` tool to discover real tag paths, then
    emits a JSON plan (AND + OR) that is executed against the deck, optionally
    scoped to a `deck` subtree.
    """
    model = get_model(model)
    started = time.perf_counter()
    debug: dict = {"model": model, "tag_lookups": [], "iterations": 0, "thinking": []}
    if not query.strip():
        return [], {}, debug
    plan = _run_planner(
        ASSIST_SYSTEM, f"Request: {query}", debug, max_iterations=max_iterations,
        model=model, on_event=on_event, cancelled=cancelled,
    )
    _check_cancelled(cancelled)
    results = _run_assist_plan(plan, limit=limit, deck=deck)
    norm_plan = _norm_plan(plan)
    debug["result_count"] = len(results)
    _search_activity(debug, f"The final search plan matched {len(results)} cards.", on_event=on_event)
    debug["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return results, norm_plan, debug


# ---------------------------------------------------------------------------
# Deep AI search — pool candidates, optionally ask the user to narrow, then
# screen each card for a precise include/exclude decision.
# ---------------------------------------------------------------------------

DEEP_TIMEOUT = 30.0
DEEP_SCREEN_THRESHOLD = 120   # above this, ask the user to narrow before screening
DEEP_SCREEN_HARD_MAX = 400    # never screen more than this in one pass
DEEP_SCREEN_BATCH = 12        # cards per screening LLM call
DEEP_SCREEN_WORKERS = 4       # parallel screening calls

DEEP_SCOUT_SYSTEM = ASSIST_SYSTEM + (
    "\n\nDEEP SEARCH MODE: a separate screening step will afterward read each "
    "pooled card and discard the mismatches, so YOUR job here is RECALL, not "
    "precision. Cast a wide net:\n"
    "- Prefer ONE distinctive filter — either a verified tag substring OR a "
    "small set of keywords — rather than stacking several. Do NOT AND a "
    "narrow keyword together with a hyper-specific tag path; that "
    "intersection usually returns nothing.\n"
    "- When the topic has synonyms or alternatives, put them in any_keywords / "
    "any_tags (OR) so more plausibly-relevant cards surface.\n"
    "- Favor a slightly broader, higher-level tag over the deepest leaf tag.\n"
    "Aim to pool a generous set of plausibly-relevant cards; the screener "
    "will tighten it."
)

SCREEN_SYSTEM = (
    "You are screening Anki flashcards against a medical student's specific "
    "request. You are given a numbered list of cards (each a short "
    "'front :: back' snippet with its topic tags). For EACH card, decide "
    "whether it genuinely matches what the student asked for. Be strict — "
    "keep a card only if it is clearly on-target; when in doubt, leave it "
    "out.\n\n"
    "Respond with ONLY a JSON object (no prose, no code fence):\n"
    '{"keep": [{"i": <card number>, "why": "<reason, max 8 words>"}, ...]}\n'
    "List only the cards to keep; omit the rest. If none match, return "
    '{"keep": []}.'
)


def _scout_user_content(intent: str, refinements: list[str]) -> str:
    parts = [f"Request: {intent}"]
    for r in refinements:
        parts.append(f"Additional narrowing the user added: {r}")
    if refinements:
        parts.append(
            "Build a plan that respects ALL of the narrowing details above."
        )
    return "\n".join(parts)


def _suggest_facets(candidates: list[dict], limit: int = 8) -> list[dict]:
    """Most common tags across the candidate pool, as narrowing suggestions.

    Skips tags shared by (nearly) every candidate — those don't discriminate.
    """
    n = len(candidates)
    counts: dict[str, int] = {}
    for c in candidates:
        for t in dict.fromkeys(c.get("tags") or []):   # de-dupe within a card
            counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict] = []
    for tag, cnt in ranked:
        if cnt < 2 or cnt >= n:          # uninformative
            continue
        out.append({"tag": tag, "label": tag.rsplit("::", 1)[-1], "count": cnt})
        if len(out) >= limit:
            break
    return out


def _relax_plan(plan: dict) -> list[dict]:
    """Build progressively looser fallback plans for when the scout's plan
    over-constrains to zero hits. Tried in order; first non-empty wins.

    1. Tags-only, ORed (drop the keyword AND that emptied the intersection).
    2. Keywords-only, ORed (drop the tags; split the AND keyword string).
    """
    tag_terms = list(dict.fromkeys(plan.get("tag_filters", []) + plan.get("any_tags", [])))
    kw_terms = list(dict.fromkeys(
        plan.get("keywords", "").split() + plan.get("any_keywords", [])
    ))
    out: list[dict] = []
    if tag_terms:
        out.append({"any_tags": tag_terms,
                    "explanation": "Relaxed to any of: " + ", ".join(tag_terms)})
    if kw_terms:
        out.append({"any_keywords": kw_terms,
                    "explanation": "Relaxed to any of: " + ", ".join(kw_terms)})
    return out


def _screen_batch(
    intent: str, refinements: list[str], batch: list[tuple[int, dict]],
    *, model: str | None = None, batch_number=1, on_event=None, cancelled=None,
) -> tuple[dict[int, str], str | None, list[dict]]:
    """Screen (index, note) entries; return kept reasons, error, and activity."""
    model = get_model(model)
    lines = [
        f"[{idx}] {_candidate_summary(note, max_len=280)}"
        for idx, note in batch
    ]
    refine_txt = (
        "\nUser narrowing: " + "; ".join(refinements) if refinements else ""
    )
    user = f"Request: {intent}{refine_txt}\n\nCards:\n" + "\n".join(lines)
    thinking = []
    try:
        _check_cancelled(cancelled)
        resp = _search_completion(
            get_client(), thinking, phase="screen", batch=batch_number,
            on_event=on_event, cancelled=cancelled,
            model=model,
            messages=[
                {"role": "system", "content": SCREEN_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_completion_tokens=1200,
            timeout=DEEP_TIMEOUT,
            **reasoning_kwargs(model),
            **reasoning_output_kwargs(model),
        )
    except TutorCancelled:
        raise
    except Exception as e:
        step = {"type": "error", "text": "This screening batch failed; its cards were not included.", "phase": "screen", "batch": batch_number}
        thinking.append(step)
        if on_event:
            on_event({"event": "tool", "step": step})
        return {}, f"{type(e).__name__}:{e}", thinking
    obj = _loose_json_obj(resp.choices[0].message.content or "")
    keep: dict[int, str] = {}
    valid_indices = {idx for idx, _ in batch}
    for item in (obj.get("keep") or []):
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if i in valid_indices:
            keep[i] = str(item.get("why") or "").strip()
    activity = {"thinking": thinking}
    _search_activity(activity, f"Screened {len(batch)} cards; kept {len(keep)}.",
                     on_event=on_event, phase="screen", batch=batch_number)
    return keep, None, thinking


def _screen_candidates(
    intent: str, refinements: list[str], pool: list[dict],
    *, model: str | None = None, on_event=None, cancelled=None,
) -> tuple[list[dict], dict]:
    """Run include/exclude screening over the pool, in parallel batches.

    Returns (kept_cards_in_pool_order_with_deep_why, debug)."""
    from concurrent.futures import ThreadPoolExecutor

    indexed = list(enumerate(pool))
    batches = [
        indexed[i : i + DEEP_SCREEN_BATCH]
        for i in range(0, len(indexed), DEEP_SCREEN_BATCH)
    ]
    keep_map: dict[int, str] = {}
    errors: list[str] = []
    thinking: list[dict] = []
    with ThreadPoolExecutor(max_workers=DEEP_SCREEN_WORKERS) as ex:
        for keep, err, steps in ex.map(
            lambda entry: _screen_batch(intent, refinements, entry[1], model=model,
                batch_number=entry[0] + 1, on_event=on_event, cancelled=cancelled), enumerate(batches)
        ):
            if err:
                errors.append(err)
            keep_map.update(keep)
            thinking.extend(steps)

    kept = [
        {**note, "deep_why": keep_map[i]}
        for i, note in indexed
        if i in keep_map
    ]
    debug = {"batches": len(batches), "thinking": thinking}
    if errors:
        debug["screen_errors"] = errors[:3]
    return kept, debug


def deep_search(
    intent: str,
    *,
    refinements: Iterable[str] = (),
    confirm: bool = False,
    deck: str = "",
    max_iterations: int = 6,
    model: str | None = None, on_event=None, cancelled=None,
) -> dict:
    """Deep, screened search with a human-in-the-loop narrowing step.

    1. Scout: the model builds an OR-aware plan (via find_tags) from the
       intent plus any user refinements, and we pool BM25 candidates.
    2. If the pool is larger than DEEP_SCREEN_THRESHOLD and the user hasn't
       confirmed, return status='clarify' with the count and suggested
       narrowing facets — the caller asks the user to narrow (or to screen
       all anyway).
    3. Screen: an LLM judges include/exclude for each pooled card; return
       status='results' with the keepers (each carrying a `deep_why`).
    """
    model = get_model(model)
    started = time.perf_counter()
    debug: dict = {"model": model, "tag_lookups": [], "iterations": 0,
                   "phase": "scout", "thinking": []}
    def finish(payload):
        debug["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return payload
    intent = (intent or "").strip()
    refinements = [str(r).strip() for r in refinements if str(r).strip()]
    if not intent:
        return finish({"status": "results", "results": [], "plan": {},
                "candidate_count": 0, "debug": debug})

    plan = _run_planner(
        DEEP_SCOUT_SYSTEM,
        _scout_user_content(intent, refinements),
        debug,
        max_iterations=max_iterations,
        model=model, on_event=on_event, cancelled=cancelled,
    )
    norm = _norm_plan(plan)

    candidates = _run_assist_plan(plan, limit=DEEP_SCREEN_HARD_MAX, deck=deck)
    _check_cancelled(cancelled)

    # Recall fallback: if the scout over-constrained to nothing, loosen the
    # plan (tags-only, then keywords-only, ORed) so the screener has cards to
    # judge instead of dead-ending.
    if not candidates:
        for rp in _relax_plan(norm):
            _check_cancelled(cancelled)
            cand = _run_assist_plan(rp, limit=DEEP_SCREEN_HARD_MAX, deck=deck)
            _search_activity(debug, f"Tried a broader search plan: {len(cand)} candidate cards.", on_event=on_event)
            if cand:
                candidates = cand
                norm = _norm_plan(rp)
                debug["relaxed"] = True
                break

    n = len(candidates)
    debug["candidate_count"] = n
    _search_activity(debug, f"Pooled {n} candidate cards for screening.", on_event=on_event)

    if n == 0:
        return finish({"status": "results", "results": [], "plan": norm,
                "candidate_count": 0, "debug": debug})

    if n > DEEP_SCREEN_THRESHOLD and not confirm:
        debug["phase"] = "clarify"
        _search_activity(debug, "Waiting for you to narrow the search or confirm screening all candidates.", on_event=on_event)
        return finish({
            "status": "clarify",
            "message": (
                f"I found {n} candidate cards for this — that's a lot to "
                "screen one by one. Add a detail to narrow it down (a "
                "subtopic, system, or exactly what you want), or screen them "
                "all anyway."
            ),
            "candidate_count": n,
            "facets": _suggest_facets(candidates),
            "plan": norm,
            "debug": debug,
        })

    pool = candidates[:DEEP_SCREEN_HARD_MAX]
    kept, screen_debug = _screen_candidates(intent, refinements, pool, model=model,
        on_event=on_event, cancelled=cancelled)
    debug["phase"] = "screen"
    debug["screened"] = len(pool)
    debug["kept"] = len(kept)
    debug["thinking"].extend(screen_debug.pop("thinking", []))
    debug.update(screen_debug)
    _search_activity(debug, f"Finished screening: kept {len(kept)} of {len(pool)} cards.", on_event=on_event, phase="screen")
    return finish({"status": "results", "results": kept, "plan": norm,
            "candidate_count": n, "debug": debug})
