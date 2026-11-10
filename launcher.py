"""Desktop app with first-run setup and an optional global shortcut.

The bundled app needs no Python or environment file. Developers can run
`python launcher.py`; their legacy .env settings remain available.
"""

from functools import partial
import json
import logging
import os
from pathlib import Path
import sys
import threading

from dotenv import load_dotenv
import webview
from webview.menu import Menu, MenuAction
from werkzeug.serving import make_server

if not getattr(sys, "frozen", False):
    load_dotenv(Path(__file__).resolve().parent / ".env")

from ankiagent.desktop import DesktopManager, create_desktop_app
from ankiagent.llm_client import available_models, get_model
from ankiagent.hotkeys import NativeHotkey

load_dotenv()

# A click on a header control must never start the frameless window's drag
# handler. Only the header background itself is a drag target.
webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True

HOTKEY = os.environ.get("HOTKEY", "<ctrl>+<alt>+<space>")
# Tutor mode is the launcher's default experience — a multi-turn tutor that
# grounds its answers in the student's deck (agentic card search + citations).
# The conversation resets whenever the window is hidden, so each hotkey-open
# starts a fresh tutor conversation.
URL = ""


window = None
visible = True
manager = None
hotkey_listener = None


def select_model(model: str):
    """Route native menu selections through the same UI state as the browser."""
    model = get_model(model)
    if window:
        window.evaluate_js(f"window.setModel({json.dumps(model)})")


def settings_menu():
    return [Menu("Settings", [MenuAction("API key & Anki deck…", open_setup), Menu("AI model", [
        MenuAction(label, partial(select_model, model))
        for model, label in available_models().items()
    ]), MenuAction("Tutor settings…", open_tutor_settings)])]


def open_tutor_settings():
    if window:
        window.evaluate_js("if (window.openTutorSettings) { window.openTutorSettings(); } "
                           "else { window.location.href = '/?mode=tutor&settings=tutor'; }")


def open_setup():
    if window:
        window.load_url(URL.split("?", 1)[0] + "setup")


def create_server(application, root: Path):
    # Reuse this user's local origin so WebKit remembers model preferences.
    port_file = root / "desktop-port"
    try:
        port = int(port_file.read_text())
        if not 1024 <= port <= 65535:
            port = 0
    except (OSError, ValueError):
        port = 0
    try:
        server = make_server("127.0.0.1", port, application, threaded=True)
    except SystemExit:
        if not port:
            raise
        server = make_server("127.0.0.1", 0, application, threaded=True)
    port_file.write_text(str(server.server_port))
    return server


def _fit_to_screen():
    """Grow the window to most of the primary screen, centered.

    Runs after the GUI starts (webview.start callback), which is the first
    point where screen dimensions are queryable. The window is created at a
    sensible default size; this enlarges it to fit whatever display it's on.
    """
    if not window:
        return
    try:
        screen = webview.screens[0]
        w, h = int(screen.width * 0.92), int(screen.height * 0.92)
        window.resize(w, h)
        window.move(int(screen.width * 0.04), int(screen.height * 0.04))
    except Exception:
        pass


def _reset_tutor():
    """Clear the Tutor-tab conversation so the next open starts fresh."""
    if not window:
        return
    try:
        window.evaluate_js(
            "if(window.resetTutor){try{window.resetTutor();}catch(e){}}"
        )
    except Exception:
        pass


def toggle():
    global visible
    if not window:
        return
    if visible:
        _reset_tutor()
        window.hide()
        visible = False
    else:
        window.show()
        window.evaluate_js(
            "const q=document.getElementById('q');"
            "if(q){q.focus();q.select();}"
            "const ti=document.getElementById('tutor-input');"
            "if(ti){ti.focus();}"
        )
        visible = True


class Api:
    def choose_collection(self):
        if not window:
            return None
        directory = Path.home() / "Library" / "Application Support" / "Anki2"
        result = window.create_file_dialog(webview.OPEN_DIALOG,
            directory=str(directory if directory.exists() else Path.home()),
            allow_multiple=False, file_types=("Anki collection (*.anki2)",))
        return str(result[0]) if result else None

    def hide(self):
        global visible
        if window and visible:
            _reset_tutor()
            window.hide()
            visible = False

    def toggle(self):
        toggle()


def _schedule_on_main(callback):
    from PyObjCTools import AppHelper
    AppHelper.callAfter(callback)


def _hotkey_pressed():
    # evaluate_js waits for WebKit; keep the Cocoa event callback nonblocking.
    threading.Thread(target=toggle, daemon=True).start()


def _apply_hotkey():
    global hotkey_listener
    try:
        enabled = manager and manager.config.get("enable_hotkey")
        if hotkey_listener:
            if enabled:
                return  # Saving unchanged settings should not restart the shortcut.
            hotkey_listener.stop()
            hotkey_listener = None
        if enabled:
            hotkey_listener = NativeHotkey(HOTKEY, _hotkey_pressed).start()
    except Exception:
        hotkey_listener = None
        logging.getLogger(__name__).exception("Could not enable the optional shortcut")


def update_hotkey():
    # Backend preparation calls this from a worker. Native registration belongs
    # on the Cocoa main queue, including after saving settings.
    _schedule_on_main(_apply_hotkey)


def main():
    global window, manager, URL
    # Load the dispatch helper on the UI thread before starting setup workers.
    from PyObjCTools import AppHelper
    manager = DesktopManager()
    manager.store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    application, _ = create_desktop_app(manager)
    server = create_server(application, manager.store.root)
    URL = f"http://127.0.0.1:{server.server_port}/?mode=tutor"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    window = webview.create_window(
        "Anki Agent",
        URL,
        js_api=Api(),
        # Opens large; _fit_to_screen() grows it to most of the display once
        # the GUI is up. min_size keeps it from being shrunk down too far.
        width=1440,
        height=900,
        min_size=(1024, 680),
        frameless=not getattr(sys, "frozen", False),
        # easy_drag hijacks the click-drag that should resize a frameless
        # window, which made resizing snap back. It's off; the top bar is a
        # `pywebview-drag-region` (see index.html) so you can still move the
        # window by dragging its header.
        easy_drag=False,
        resizable=True,
        background_color="#111",
    )
    manager.on_ready = update_hotkey
    manager.startup()
    try:
        webview.start(_fit_to_screen,
            icon=str(Path(__file__).resolve().parent / "ankiagent" / "static" / "icon.svg"),
            menu=settings_menu(), private_mode=False,
            storage_path=str(manager.store.root / "webview"))
    finally:
        if hotkey_listener:
            hotkey_listener.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
