"""Desktop launcher: wraps Flask in a frameless pywebview window bound to a global hotkey.

Run `python launcher.py`. Press the hotkey (default option+space) to toggle the window.
Configure via .env: PORT (default 5050), HOTKEY (pynput format, default <alt>+<space>).
"""

import os
import threading
import time
import urllib.request

from dotenv import load_dotenv
import webview
from pynput import keyboard

from ankiagent.app import app

load_dotenv()

PORT = int(os.environ.get("PORT", "5050"))
HOTKEY = os.environ.get("HOTKEY", "<alt>+<space>")
# Tutor mode is the launcher's default experience — a multi-turn tutor that
# grounds its answers in the student's deck (agentic card search + citations).
# The conversation resets whenever the window is hidden, so each hotkey-open
# starts a fresh tutor conversation.
URL = f"http://127.0.0.1:{PORT}/?mode=tutor"


def run_flask():
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def wait_ready(timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=0.3)
            return True
        except Exception:
            time.sleep(0.1)
    return False


window = None
visible = True


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
    def hide(self):
        global visible
        if window and visible:
            _reset_tutor()
            window.hide()
            visible = False

    def toggle(self):
        toggle()


# pynput 1.8.x on macOS has a bug where the media-key branch of
# _handle_message invokes on_press/on_release with one argument, but
# GlobalHotKeys._on_press/_on_release require (key, injected). Without this
# shim, pressing volume / brightness / play-pause keys spams TypeErrors.
class _CompatGlobalHotKeys(keyboard.GlobalHotKeys):
    def _on_press(self, key, injected=False):
        return super()._on_press(key, injected)

    def _on_release(self, key, injected=False):
        return super()._on_release(key, injected)


def start_hotkey():
    _CompatGlobalHotKeys({HOTKEY: toggle}).start()


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    if not wait_ready():
        raise SystemExit(f"Flask did not come up on {URL}")
    start_hotkey()
    window = webview.create_window(
        "Anki Agent",
        URL,
        js_api=Api(),
        # Opens large; _fit_to_screen() grows it to most of the display once
        # the GUI is up. min_size keeps it from being shrunk down too far.
        width=1440,
        height=900,
        min_size=(1024, 680),
        frameless=True,
        # easy_drag hijacks the click-drag that should resize a frameless
        # window, which made resizing snap back. It's off; the top bar is a
        # `pywebview-drag-region` (see index.html) so you can still move the
        # window by dragging its header.
        easy_drag=False,
        resizable=True,
        background_color="#111",
    )
    webview.start(_fit_to_screen)
