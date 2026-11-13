"""Frozen app entry point; keep logs and mutable files outside the bundle."""
from pathlib import Path
import json
import os
import sys
import tempfile


def smoke_test(report: Path):
    from werkzeug.test import Client
    from werkzeug.wrappers import Response
    from ankiagent.desktop import DesktopManager, create_desktop_app
    from ankiagent.settings import SettingsStore
    import ankiagent
    import openai
    from ankiagent.llm_client import OPENROUTER_BASE_URL
    with openai.OpenAI(api_key="sk-or-smoke-not-real", base_url=OPENROUTER_BASE_URL) as client:
        assert str(client.base_url).startswith(OPENROUTER_BASE_URL)
    import importlib.util
    assert importlib.util.find_spec("webview.platforms.cocoa") is not None
    root = Path(ankiagent.__file__).parent
    for relative in ("templates/setup.html", "templates/index.html", "static/setup.js", "static/app.js", "static/app.css"):
        assert (root / relative).is_file(), relative
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        store = SettingsStore(temporary / "preferences")
        store.vault.set("gsk_local_smoke_test_only")
        assert store.get_key() == "gsk_local_smoke_test_only"
        assert store.vault.path.stat().st_mode & 0o777 == 0o600
        manager = DesktopManager(store=store)
        application, _ = create_desktop_app(manager)
        client = Client(application, Response)
        page = client.get("/setup", base_url="http://127.0.0.1")
        assert page.status_code == 200
        assert b"Set up Anki Agent" in page.data
        assert client.get("/api/setup/state", base_url="http://127.0.0.1").status_code == 403
        # Exercise the packaged indexer, SQLite extension, templates, and deck
        # changes using only synthetic local data and a disposable .env key.
        import sqlite3
        collection = temporary / "collection.anki2"
        with sqlite3.connect(collection) as connection:
            connection.executescript("""
                CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT);
                INSERT INTO fields VALUES (1,0,'Front'),(1,1,'Back');
                CREATE TABLE decks (id INTEGER, name TEXT);
                INSERT INTO decks VALUES (1,'Study'),(2,'Study'||char(31)||'GI');
                CREATE TABLE cards (nid INTEGER, did INTEGER);
                INSERT INTO cards VALUES (1,1),(2,2);
                CREATE TABLE notes (id INTEGER, guid TEXT, flds TEXT, tags TEXT, mid INTEGER);
                INSERT INTO notes VALUES (1,'first','First sample card',' sample ',1),(2,'second','Second sample card',' sample ',1);
            """)
        original = collection.read_bytes()
        for deck, expected in (("Study", 2), ("Study::GI", 1)):
            manager.config = {"deck_path": str(collection), "deck_name": deck, "enable_hotkey": False}
            manager.store.save(manager.config)
            manager._prepare()
            assert manager.status == "ready", manager.message
            result = client.get("/api/stats", base_url="http://127.0.0.1")
            assert result.get_json()["notes"] == expected, result.data
            result.close()
        # Both provider keys and the reloaded model catalog must also work in
        # the frozen app. Synthetic keys only; no inference/network request.
        store.vault.set("sk-or-smoke-not-real", "openrouter")
        manager.config["provider"] = "openrouter"
        store.save(manager.config)
        manager._prepare()
        assert manager.status == "ready", manager.message
        page = client.get("/", base_url="http://127.0.0.1")
        assert b'openai/gpt-6-luna' in page.data
        assert b'google/gemini-3.8-flash' in page.data
        assert b'openai/gpt-oss-120b' in page.data
        assert b'<optgroup label="OpenRouter">' in page.data
        assert b'google/gemini-2.5-flash-lite' not in page.data
        page.close()
        manager.config["provider"] = "groq"
        store.save(manager.config)
        manager._prepare()
        assert manager.status == "ready", manager.message
        assert collection.read_bytes() == original, "Source collection was modified"
        page = client.get("/", base_url="http://127.0.0.1")
        assert b"setup-settings" in page.data
        page.close()
    report.write_text(json.dumps({"ok": True, "architecture": __import__("platform").machine(), "setup": True, "native_webview_module": True, "headless": True, "indexing": True, "deck_switching": True, "source_read_only": True, "openrouter": True}))


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        report = Path(sys.argv[sys.argv.index("--report") + 1])
        smoke_test(report)
    else:
        from ankiagent.settings import data_dir
        logs = data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "desktop.log").open("a", buffering=1) as stream:
            sys.stdout = sys.stderr = stream
            from launcher import main
            main()
