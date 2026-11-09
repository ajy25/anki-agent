"""Desktop onboarding stays available even before a deck can be indexed."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import secrets
import threading

from flask import Flask, jsonify, render_template, request
from werkzeug.wsgi import ClosingIterator

from .settings import SettingsStore, discover_profiles
from .parse import list_anki_decks, looks_like_anki_collection


def validate_key(key: str) -> None:
    if not key.startswith("gsk_") or any(char.isspace() for char in key):
        raise ValueError("Enter a Groq API key starting with gsk_.")
    from groq import Groq
    try:
        with Groq(api_key=key, timeout=10, max_retries=0) as client:
            client.models.list()
    except Exception as error:
        code = getattr(error, "status_code", None)
        if code in (401, 403):
            raise ValueError("Groq did not accept this key. Check it or create a new one.") from None
        raise ValueError("Could not reach Groq to check the key. Check your connection and try again.") from None


def collection_decks(raw: str) -> tuple[Path, list[str]]:
    path = Path(raw).expanduser().resolve()
    if not raw or not path.is_file():
        raise ValueError("Choose your Anki collection file first.")
    if path.suffix.lower() in (".apkg", ".colpkg"):
        raise ValueError("Choose collection.anki2 from an Anki profile, rather than an exported package.")
    if not looks_like_anki_collection(path):
        raise ValueError("This is not an Anki collection. Choose your profile's collection.anki2 file.")
    try:
        decks = list_anki_decks(path, include_subdecks=True)
    except Exception:
        raise ValueError("Could not read this Anki collection. Try syncing Anki, then choose it again.") from None
    return path, decks


class DesktopManager:
    def __init__(self, store=None, backend_factory=None):
        self.store = store or SettingsStore()
        self.config = self.store.read() or {
            "deck_path": os.environ.get("DECK_PATH", ""),
            "deck_name": os.environ.get("DECK_NAME", ""), "enable_hotkey": bool(os.environ.get("HOTKEY")),
        }
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.requests_done = threading.Condition(self.lock)
        self.active_requests = 0
        self.state_lock = threading.Lock()
        self.status = "setup"
        self.message = ""
        self.key_available = bool(os.environ.get("GROQ_API_KEY"))
        self.backend = None
        self.backend_factory = backend_factory or self._load_backend
        self.on_ready = None

    def state(self):
        with self.state_lock:
            return {"status": self.status, "message": self.message, "config": self.config,
                    "has_key": self.key_available, "profiles": discover_profiles()}

    def _set_status(self, status, message=""):
        with self.state_lock:
            self.status, self.message = status, message

    def startup(self):
        if self.config.get("deck_path"):
            self.prepare()

    def prepare(self):
        with self.state_lock:
            if self.status == "indexing":
                raise ValueError("Your deck is still being prepared. Please wait.")
            self.status, self.message = "indexing", "Preparing your deck…"
        threading.Thread(target=self._prepare, daemon=True).start()

    def _prepare(self):
        try:
            with self.lock:
                while self.active_requests:
                    self.requests_done.wait()
                path, decks = collection_decks(self.config.get("deck_path", ""))
                name = self.config.get("deck_name") or ""
                if name and name not in decks:
                    raise ValueError("That deck is no longer in this profile. Choose a deck again.")
                key = self.store.vault.get() if self.store.path.exists() else os.environ.get("GROQ_API_KEY", "")
                self.key_available = bool(key)
                if not key:
                    raise ValueError("Add your Groq API key to finish setup.")
                os.environ.update(DECK_PATH=str(path), DECK_NAME=name, GROQ_API_KEY=key,
                    LLM_PROVIDER="groq", ANKIAGENT_CACHE_DIR=str(self.store.root / "cache"))
                os.environ.setdefault("LLM_MODEL", "openai/gpt-oss-120b")
                self.backend = self.backend_factory(path, name)
            self._set_status("ready")
            if self.on_ready:
                self.on_ready()
        except Exception as error:
            self._set_status("setup", str(error))

    def _load_backend(self, path, name):
        from . import ingest, llm_client, search
        from .media import resolve_media_dir
        ingest.DECK_PATH, ingest.DECK_NAME = path, name or None
        ingest.CACHE_DIR = self.store.root / "cache"
        ingest.DB_PATH = ingest.CACHE_DIR / "anki.db"
        search.DB_PATH = ingest.DB_PATH
        search.reset_conn()
        # The first import builds the index here, on the background thread.
        module = importlib.import_module("ankiagent.app")
        ingest.ensure_built()
        module.app.config["MEDIA_DIR"] = resolve_media_dir(path)
        llm_client._GROQ_CLIENT = None
        llm_client._GROQ_ASYNC_CLIENT = None
        return module.app


def create_desktop_app(manager: DesktopManager, key_validator=validate_key):
    setup = Flask(__name__, static_folder="static", static_url_path="/setup-assets", template_folder="templates")

    @setup.before_request
    def local_requests_only():
        if request.host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
            return jsonify(error="Local requests only."), 403
        if request.path.startswith("/api/setup/"):
            if not secrets.compare_digest(request.headers.get("X-AnkiAgent-Setup-Token", ""), manager.token):
                return jsonify(error="Reopen the setup screen and try again."), 403

    @setup.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @setup.route("/")
    @setup.route("/setup")
    def index():
        return render_template("setup.html", token=manager.token)

    @setup.get("/api/setup/state")
    def state():
        return jsonify(manager.state())

    @setup.post("/api/setup/decks")
    def decks():
        try:
            path, names = collection_decks(str((request.get_json(silent=True) or {}).get("path", "")))
            return jsonify(path=str(path), decks=names)
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @setup.post("/api/setup/test-key")
    def test_key():
        key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
        try:
            if not key:
                key = manager.store.vault.get() if manager.store.path.exists() else os.environ.get("GROQ_API_KEY", "")
            key_validator(key)
            return jsonify(ok=True)
        except (ValueError, RuntimeError) as error:
            return jsonify(error=str(error)), 400

    @setup.post("/api/setup/save")
    def save():
        data = request.get_json(silent=True) or {}
        try:
            if manager.status == "indexing":
                raise ValueError("Your deck is still being prepared. Please wait.")
            path, names = collection_decks(str(data.get("path", "")))
            name = str(data.get("deck", "")).strip()
            if name and name not in names:
                raise ValueError("Choose a deck from the list.")
            key = str(data.get("key", "")).strip()
            existing = manager.store.vault.get() if manager.store.path.exists() else os.environ.get("GROQ_API_KEY", "")
            if key:
                key_validator(key)
            elif not existing:
                raise ValueError("Add your Groq API key first.")
            config = {"deck_path": str(path), "deck_name": name,
                      "enable_hotkey": data.get("enable_hotkey") is True}
            manager.store.save(config, key or existing)
            manager.config = config
            manager.key_available = True
            manager.prepare()
            return jsonify(ok=True)
        except (ValueError, RuntimeError, OSError) as error:
            return jsonify(error=str(error)), 400

    class DesktopApp:
        def __call__(self, environ, start_response):
            if environ.get("HTTP_HOST", "").split(":", 1)[0] not in ("127.0.0.1", "localhost"):
                return setup.response_class("Local requests only.", status=403)(environ, start_response)
            path = environ.get("PATH_INFO", "/")
            if path == "/setup" or path.startswith(("/api/setup/", "/setup-assets/")):
                return setup(environ, start_response)
            if manager.status != "ready":
                return setup(environ, start_response)
            with manager.lock:
                backend = manager.backend if manager.status == "ready" else None
                if backend is not None:
                    manager.active_requests += 1
            if backend is None:
                return setup(environ, start_response)
            def release():
                with manager.lock:
                    manager.active_requests -= 1
                    manager.requests_done.notify_all()
            try:
                result = backend(environ, start_response)
                # A deck change waits for active streamed answers to finish.
                return ClosingIterator(result, release)
            except BaseException:
                release()
                raise

    return DesktopApp(), setup
