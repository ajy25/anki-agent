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
from .providers import PROVIDERS, desktop_provider
from .parse import list_anki_decks, looks_like_anki_collection


def validate_key(key: str, provider: str = "groq") -> None:
    provider = desktop_provider(provider)
    info = PROVIDERS[provider]
    label, prefix = info["label"], info["prefix"]
    if not key.startswith(prefix) or any(char.isspace() for char in key):
        raise ValueError(f"Enter your {label} API key starting with {prefix}.")
    try:
        if provider == "openrouter":
            # /models is public and cannot validate a key. /key authenticates
            # without spending credits or sending any deck content.
            import httpx
            with httpx.Client(timeout=10) as client:
                response = client.get("https://openrouter.ai/api/v1/key",
                                      headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                if not isinstance(response.json().get("data"), dict):
                    raise RuntimeError("Invalid key response")
        else:
            from groq import Groq
            with Groq(api_key=key, timeout=10, max_retries=0) as client:
                client.models.list()
    except Exception as error:
        code = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
        if code in (401, 403):
            raise ValueError(f"{label} did not accept this key. Check it or create a new one.") from None
        raise ValueError(f"Could not reach {label} to check the key. Check your connection and try again.") from None


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
            "provider": os.environ.get("LLM_PROVIDER", "groq"),
        }
        try:
            self.config["provider"] = desktop_provider(self.config.get("provider"))
        except ValueError:
            self.config["provider"] = "groq"
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.requests_done = threading.Condition(self.lock)
        self.active_requests = 0
        self.state_lock = threading.Lock()
        self.status = "setup"
        self.message = ""
        try:
            self.key_available = bool(self.store.get_key(self.config["provider"]))
        except RuntimeError:
            self.key_available = False
        self.backend = None
        self.backend_factory = backend_factory or self._load_backend
        self.on_ready = None

    def state(self):
        keys = {}
        for provider in PROVIDERS:
            try:
                keys[provider] = bool(self.store.get_key(provider))
            except RuntimeError:
                keys[provider] = False
        with self.state_lock:
            return {"status": self.status, "message": self.message, "config": self.config,
                    "has_key": any(keys.values()), "saved_keys": keys,
                    "providers": PROVIDERS, "profiles": discover_profiles()}

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
                keys = {p: self.store.get_key(p) for p in PROVIDERS}
                available = [p for p, key in keys.items() if key]
                self.key_available = bool(available)
                if not available:
                    raise ValueError("Add a Groq or OpenRouter API key to finish setup.")
                provider = self.config.get("provider")
                if provider not in available:
                    provider = available[0]
                os.environ.pop("LLM_MODEL", None)
                os.environ.update(DECK_PATH=str(path), DECK_NAME=name, LLM_PROVIDER=provider,
                    ANKIAGENT_PROVIDERS=",".join(available), ANKIAGENT_CACHE_DIR=str(self.store.root / "cache"))
                for p, info in PROVIDERS.items():
                    os.environ[info["key_env"]] = keys[p]
                from .llm_client import reset_clients
                reset_clients()
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
        llm_client.reset_clients()
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
        return render_template("setup.html", token=manager.token, providers=PROVIDERS)

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
        data = request.get_json(silent=True) or {}
        key = str(data.get("key", "")).strip()
        try:
            provider = desktop_provider(data.get("provider") or manager.config.get("provider"))
            if not key:
                key = manager.store.get_key(provider)
            key_validator(key, provider)
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
            # Accept both key fields in one save. Validate every new key before
            # writing either, and retain saved keys when their fields are blank.
            submitted = data.get("keys")
            if submitted is None:  # compatibility with older setup pages
                provider = desktop_provider(data.get("provider") or manager.config.get("provider"))
                submitted = {provider: data.get("key", "")}
            if not isinstance(submitted, dict) or any(p not in PROVIDERS for p in submitted):
                raise ValueError("Use the Groq and OpenRouter key fields in Setup.")
            new_keys = {p: str(value).strip() for p, value in submitted.items() if str(value).strip()}
            for p, key in new_keys.items():
                key_validator(key, p)
            available = []
            effective_keys = {}
            for p in PROVIDERS:
                try:
                    has_key = new_keys.get(p) or manager.store.get_key(p)
                except RuntimeError:
                    if not new_keys:
                        raise
                    has_key = False
                if has_key:
                    available.append(p)
                    effective_keys[p] = has_key
            if not available:
                raise ValueError("Add at least one API key: Groq or OpenRouter.")
            provider = manager.config.get("provider")
            if provider not in available:
                provider = available[0]
            config = {"deck_path": str(path), "deck_name": name,
                      "enable_hotkey": data.get("enable_hotkey") is True, "provider": provider}
            manager.store.save(config, keys=effective_keys)
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
