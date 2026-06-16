"""Run Anki Agent in the browser: `python run.py`, then open http://127.0.0.1:5050.

Builds the deck index on first launch (see .env: DECK_PATH / DECK_NAME).
For the desktop global-hotkey window instead, run `python launcher.py`.
"""

from ankiagent.app import main

if __name__ == "__main__":
    main()
