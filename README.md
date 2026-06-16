# Anki Agent

**Ask your Anki deck questions and get answers backed by your own cards.**

Anki Agent sits on top of the deck you already study — the AnKing Step deck, or any
other — and turns it into something you can *talk to*:

- 🔎 **Search** your deck instantly by keyword, topic, or tag.
- ✨ **AI search** — type what you want in plain English ("high-yield cards on a
  topic") and let it find the right cards.
- 🎓 **Tutor** — a study buddy that explains concepts and shows you the exact
  cards behind every point, so you can trust (and review) what it says.

Everything is grounded in *your* deck, so the answers match what you're actually
expected to know — no made-up facts, and every claim links back to a card.

> Your Anki data is read **read-only**. Anki Agent never edits, moves, or syncs your
> cards. It's safe to use whether Anki is open or closed.

---

## What you'll need

1. **Anki** installed, with the deck you study (e.g. the AnKing Step deck).
2. **Python 3.10 or newer** — check by running `python --version` in a terminal.
   ([Download here](https://www.python.org/downloads/) if you don't have it.)
3. **A free Groq API key** for the AI features — sign up at
   [console.groq.com](https://console.groq.com/) and create a key (starts with `gsk_`).

You do **not** need to know how to code. The steps below are copy-paste.

---

## Setup (about 5 minutes)

Open a terminal, then go to this project's folder:

```bash
cd path/to/anki-agent
```

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Tell it about your deck and key

Copy the example settings file, then open `.env` in any text editor and fill it in:

```bash
cp .env.example .env
```

You only need to set two things (the file explains each one):

- **`GROQ_API_KEY`** — your key from console.groq.com.
- **`DECK_PATH`** — where your Anki collection lives. On most Macs it's already
  filled in correctly:
  `~/Library/Application Support/Anki2/User 1/collection.anki2`
  (change `User 1` if your Anki profile has a different name).

Optionally set **`DECK_NAME`** to study one deck — type it exactly as it appears
in Anki's deck list (e.g. `AnKing Step Deck`). Leave it blank to include
everything.

> 💡 Not sure of your deck's name? Just run the app (next step) — if the name
> doesn't match, it prints the list of decks it found so you can copy the right one.

### 3. Run it

```bash
python run.py
```

Then open **http://127.0.0.1:5050** in your browser. The first launch spends a
few seconds indexing your deck; after that it's instant.

That's it — start searching or switch to **Tutor** and ask a question.

---

## Using it day to day

- **Switch modes** with the tabs at the top: Search · Tutor.
- The deck you're studying is shown as a pill next to the title.
- In **Search**, click any tag in the sidebar to filter, or flip on **AI assist**
  to search in plain English.
- In **Tutor**, every answer cites the cards it used — click a citation to jump
  to that card.

**Your deck changed?** Anki Agent re-indexes automatically the next time you start it.
To force a refresh without restarting:

```bash
python -m ankiagent.ingest --force
```

**Want a quick-access window instead of a browser tab?** (macOS) Run
`python launcher.py` to get a floating window you summon with a global hotkey
(default **Ctrl+Alt+Space**).

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `DECK_PATH is not set` | You haven't created `.env` yet, or `DECK_PATH` is blank. See step 2. |
| `No Anki deck named '…'` | The `DECK_NAME` doesn't match. The error lists the real deck names — copy one in. |
| `Deck file not found` | Double-check `DECK_PATH`. Replace `User 1` with your actual Anki profile name. |
| AI features error out | Check your `GROQ_API_KEY` is correct and your internet is on. |
| It only shows text, no images | That's expected — Anki Agent indexes the text of your cards, not images or audio. |

---

## For the curious (how it works)

Anki Agent reads your Anki collection (a local database), builds a fast search index
of your cards' text and tags, and serves a small web app. The AI features call
Groq and use tools to look through *your* deck before answering, so nothing is
hard-coded to any one deck.

```
run.py            Start the browser app
launcher.py       Start the desktop hotkey window (macOS)
ankiagent/          The app
  app.py            Web server + API
  ingest.py         Builds the search index from your deck
  parse.py          Reads your Anki collection / TSV export
  search.py         Keyword + tag search
  llm.py            AI search and tutor
  llm_client.py     Talks to Groq / OpenAI
  templates/        Web page
  static/           Styles + scripts
```

Advanced users: Anki Agent can also read a TSV export (`.txt`) instead of the live
collection — just point `DECK_PATH` at it. All settings are documented in
[`.env.example`](.env.example).
