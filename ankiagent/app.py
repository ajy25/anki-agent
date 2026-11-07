"""Flask app for the Anki Agent interface.

Routes
------
GET  /                  HTML shell
GET  /api/search        q=, limit=, tags=  (BM25 deck search)
POST /api/search_assist  plain-English -> tag/keyword search plan
POST /api/deep_search   pooled candidates -> per-card screening (human-in-loop)
GET  /api/note/<guid>   full note by GUID
GET  /api/tags          top tags (count-desc)
GET  /api/decks         deck/subdeck hierarchy with note counts
GET  /api/stats         index stats
GET  /media            filename=  (images from Anki's media folder)
POST /api/tutor         multi-turn deck-grounded tutor (agentic, cited)
"""

from __future__ import annotations

import os
import json
import queue
import threading
from pathlib import Path

import bleach
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from .ingest import DECK_PATH, ensure_built
from .media import IMAGE_TYPES, is_media_filename, resolve_media_dir, rewrite_image_sources
from .parse import DISPLAY_ALLOWED_ATTRS, DISPLAY_ALLOWED_TAGS
from . import search
from .llm_client import available_models, get_model

load_dotenv()

app = Flask(
    __name__,
    static_folder="static",
    template_folder="templates",
)
app.config["MEDIA_DIR"] = resolve_media_dir(DECK_PATH)

# Build the index on startup if needed.
_stats = ensure_built(force=False)
if _stats:
    app.logger.info(
        "Built index: %d notes, %d tags in %.1fs",
        _stats["notes"], _stats["tags"], _stats["seconds"],
    )


def _sanitize(html_str: str) -> str:
    if not html_str:
        return ""
    cleaned = bleach.clean(
        html_str,
        tags=DISPLAY_ALLOWED_TAGS,
        attributes=DISPLAY_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    return rewrite_image_sources(cleaned)


def _present(note: dict) -> dict:
    """Apply bleach to HTML fields before sending to the client."""
    out = dict(note)
    out["front_html"] = _sanitize(note.get("front_html", ""))
    out["back_html"] = _sanitize(note.get("back_html", ""))
    out["extras_html"] = [_sanitize(x) for x in note.get("extras_html", [])]
    return out


@app.route("/")
def index():
    return render_template("index.html", models=available_models(), default_model=get_model())


@app.route("/media")
def media_image():
    """Serve only image files inside the configured Anki media directory."""
    filename = request.args.get("filename", "")
    if not is_media_filename(filename):
        abort(404)
    mimetype = IMAGE_TYPES.get(Path(filename).suffix.lower())
    if not mimetype:
        abort(404)
    root = Path(app.config["MEDIA_DIR"]).resolve()
    image = (root / filename).resolve()
    if not image.is_relative_to(root) or not image.is_file():
        abort(404)
    response = send_file(image, mimetype=mimetype, conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # SVGs may contain active content; allow their embedded images and styles,
    # while preventing scripts even if a file is opened directly in a tab.
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
    )
    return response


@app.route("/api/stats")
def api_stats():
    return jsonify(search.stats())


@app.route("/api/tags")
def api_tags():
    limit = int(request.args.get("limit", 200))
    limit = max(1, min(limit, 1000))
    return jsonify([{"tag": t, "count": c} for t, c in search.top_tags(limit)])


@app.route("/api/decks")
def api_decks():
    """Deck/subdeck hierarchy (nested, with inclusive note counts)."""
    return jsonify(search.deck_tree())


@app.route("/api/sidebar_tags")
def api_sidebar_tags():
    """Most-used deck tags for the sidebar quick-filter list."""
    limit = int(request.args.get("limit", 40))
    limit = max(1, min(limit, 200))
    return jsonify(search.sidebar_tags(limit))


@app.route("/api/note/<guid>")
def api_note(guid: str):
    note = search.get_note(guid)
    if not note:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_present(note))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 30))
    limit = max(1, min(limit, 100))
    exact_tags = [t for t in request.args.get("tags", "").split(",") if t.strip()]
    deck = request.args.get("deck", "").strip()

    if not q and not exact_tags and not deck:
        return jsonify({"mode": "fast", "query": q, "results": [], "note": "empty"})

    results = search.fast_search(q, limit=limit, exact_tags=exact_tags, deck=deck)
    presented = [_present(r) for r in results]
    return jsonify({
        "mode": "fast",
        "query": q,
        "tags": exact_tags,
        "deck": deck,
        "results": presented,
        "debug": {},
    })


@app.route("/api/search_assist", methods=["POST"])
def api_search_assist():
    """AI-assisted search: plain-English request -> tag/keyword plan.

    Body: {"q": "<plain english>", "limit": <int>}
    Returns {query, plan, results, guids, debug}. `guids` is every
    matching note id, in result order, for the bulk Anki-search button.
    """
    data = request.get_json(silent=True) or {}
    try:
        model = get_model(data.get("model"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    q = str(data.get("q", "")).strip()
    deck = str(data.get("deck", "")).strip()
    # AI-assisted search is uncapped: return every matching card. The ceiling
    # is a safety bound far above any real deck size, not a result limit.
    limit = int(data.get("limit", 100000))
    limit = max(1, min(limit, 100000))
    if not q:
        return jsonify({"query": q, "plan": {}, "results": [],
                        "guids": [], "debug": {"error": "empty"}})
    if data.get("stream") is True:
        def run(emit, cancelled):
            from .llm import assisted_search
            results, plan, debug = assisted_search(q, limit=limit, deck=deck, model=model,
                on_event=emit, cancelled=cancelled)
            return {"query": q, "plan": plan, "results": [_present(r) for r in results],
                "guids": [r.get("guid") for r in results if r.get("guid")],
                "ids": [r.get("nid") for r in results if r.get("nid")], "debug": debug}
        return _stream_response(run, "AI search failed. Please try again.")
    try:
        from .llm import assisted_search
        results, plan, debug = assisted_search(q, limit=limit, deck=deck, model=model)
    except Exception as e:
        app.logger.warning("assisted_search failed: %s", e)
        return jsonify({"query": q, "plan": {}, "results": [],
                        "guids": [], "debug": {"error": str(e)}}), 500
    presented = [_present(r) for r in results]
    return jsonify({
        "query": q,
        "plan": plan,
        "results": presented,
        "guids": [r.get("guid") for r in results if r.get("guid")],
        "ids": [r.get("nid") for r in results if r.get("nid")],
        "debug": debug,
    })


@app.route("/api/deep_search", methods=["POST"])
def api_deep_search():
    """Deep AI search: pool candidates, optionally ask the user to narrow,
    then screen each card for a precise include/exclude.

    Body: {"q": "<plain english>", "refinements": ["..."], "confirm": <bool>}
    Returns either:
      {status:"clarify", message, candidate_count, facets, plan, debug}
    or
      {status:"results", results, guids, candidate_count, plan, debug}
    """
    data = request.get_json(silent=True) or {}
    try:
        model = get_model(data.get("model"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    q = str(data.get("q", "")).strip()
    raw_refs = data.get("refinements") or []
    refinements = [
        str(r).strip() for r in raw_refs
        if isinstance(r, str) and str(r).strip()
    ]
    confirm = bool(data.get("confirm"))
    deck = str(data.get("deck", "")).strip()
    if not q:
        return jsonify({"status": "results", "query": q, "results": [],
                        "guids": [], "plan": {}, "debug": {"error": "empty"}})
    if data.get("stream") is True:
        def run(emit, cancelled):
            from .llm import deep_search
            out = deep_search(q, refinements=refinements, confirm=confirm, deck=deck, model=model,
                on_event=emit, cancelled=cancelled)
            results = out.get("results") or []
            return {**out, "query": q,
                "results": [{**_present(r), "deep_why": r.get("deep_why", "")} for r in results],
                "guids": [r.get("guid") for r in results if r.get("guid")],
                "ids": [r.get("nid") for r in results if r.get("nid")]}
        return _stream_response(run, "Deep search failed. Please try again.")
    try:
        from .llm import deep_search
        out = deep_search(q, refinements=refinements, confirm=confirm, deck=deck, model=model)
    except Exception as e:
        app.logger.warning("deep_search failed: %s", e)
        return jsonify({"status": "error", "query": q, "results": [],
                        "guids": [], "plan": {}, "debug": {"error": str(e)}}), 500

    if out.get("status") == "clarify":
        return jsonify({
            "status": "clarify",
            "query": q,
            "message": out.get("message", ""),
            "candidate_count": out.get("candidate_count"),
            "facets": out.get("facets", []),
            "plan": out.get("plan", {}),
            "debug": out.get("debug", {}),
        })

    results = out.get("results") or []
    presented = [{**_present(r), "deep_why": r.get("deep_why", "")} for r in results]
    return jsonify({
        "status": "results",
        "query": q,
        "plan": out.get("plan", {}),
        "results": presented,
        "guids": [r.get("guid") for r in results if r.get("guid")],
        "ids": [r.get("nid") for r in results if r.get("nid")],
        "candidate_count": out.get("candidate_count"),
        "debug": out.get("debug", {}),
    })


def _stream_response(run, failure_message: str) -> Response:
    """Bridge synchronous inference to SSE with backpressure and cancellation."""
    from .llm import TutorCancelled
    events = queue.Queue(maxsize=64)
    cancelled = threading.Event()

    def emit(event):
        while not cancelled.is_set():
            try:
                events.put(event, timeout=0.25)
                return
            except queue.Full:
                continue
        raise TutorCancelled()

    def work():
        try:
            payload = run(emit, cancelled)
            debug = payload.get("debug") or {}
            if debug.get("error") or debug.get("finalize_error"):
                emit({**payload, "event": "error", "error": failure_message})
            else:
                emit({**payload, "event": "done"})
        except TutorCancelled:
            pass
        except Exception as error:
            app.logger.warning("Streaming request failed: %s", error)
            if not cancelled.is_set():
                try:
                    emit({"event": "error", "error": failure_message})
                except TutorCancelled:
                    pass

    def generate():
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            yield 'event: start\ndata: {}\n\n'
            while not cancelled.is_set():
                try:
                    event = events.get(timeout=10)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
                if event["event"] in ("done", "error"):
                    return
        finally:
            cancelled.set()

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })
    response.call_on_close(cancelled.set)
    return response


def _stream_tutor(messages: list[dict], *, deck: str, model: str) -> Response:
    def run(emit, cancelled):
        from .llm import tutor_chat
        answer, sources, debug = tutor_chat(messages, deck=deck, model=model,
            on_event=emit, cancelled=cancelled)
        return {"answer": answer, "sources": [
            {**_present(s), "ref": s.get("ref"), "cited": s.get("cited", False)} for s in sources
        ], "debug": debug}
    return _stream_response(run, "The tutor couldn't produce an answer after multiple attempts. Please try again shortly.")


@app.route("/api/tutor", methods=["POST"])
def api_tutor():
    """Agentic, deck-grounded tutor. Searches the deck and cites cards.

    Body: {"messages": [{"role": "user"|"assistant", "content": "..."}, ...]}
    The last message must be from the user (the current turn); earlier ones
    are prior conversation turns used for follow-up context.
    Returns: {"answer", "sources", "debug"} or {"error": ...}.
    """
    data = request.get_json(silent=True) or {}
    try:
        model = get_model(data.get("model"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    raw = data.get("messages") or []
    # Validate the actual current turn before cleaning history. Skipping an
    # empty/malformed last turn must not silently replay an earlier question.
    if (
        not isinstance(raw, list) or not raw or not isinstance(raw[-1], dict)
        or raw[-1].get("role") != "user"
        or not isinstance(raw[-1].get("content"), str)
        or not raw[-1]["content"].strip()
    ):
        return jsonify({"error": "need_user_message"}), 400
    cleaned: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content", "")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content})
    if not cleaned or cleaned[-1]["role"] != "user":
        return jsonify({"error": "need_user_message"}), 400
    deck = str(data.get("deck", "")).strip()
    if data.get("stream") is True:
        return _stream_tutor(cleaned, deck=deck, model=model)
    try:
        from .llm import tutor_chat
        answer, sources, debug = tutor_chat(cleaned, deck=deck, model=model)
    except Exception as e:
        app.logger.warning("tutor_chat failed: %s", e)
        return jsonify({"error": str(e)}), 500
    if debug.get("error") or debug.get("finalize_error"):
        app.logger.warning(
            "Tutor failed after retries: model=%s retries=%d error=%s",
            model, debug.get("response_retries", 0),
            debug.get("error") or debug.get("finalize_error"),
        )
        return jsonify({
            "error": "The tutor couldn't produce an answer after multiple attempts. Please try again shortly.",
            "sources": [], "debug": debug,
        }), 502
    presented = [
        {**_present(s), "ref": s.get("ref"), "cited": s.get("cited", False)}
        for s in sources
    ]
    return jsonify({"answer": answer, "sources": presented, "debug": debug})


def main() -> None:
    """Run the dev server (browser workflow). See run.py / launcher.py."""
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=True)


if __name__ == "__main__":
    main()
