# Run or develop Anki Agent from source

Use this guide to run and develop the source code. For the installed app, see the **[Mac setup guide](MAC_SETUP.md)**.

## Install dependencies

Use Python 3.12 for the Mac packaging environment. From a checkout of this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Desktop development on macOS

```sh
python launcher.py
```

The setup screen accepts either or both Groq and OpenRouter keys, Anki profile, and deck selection. It saves the key in a private, plaintext `.env` file (mode `0600`) and preferences in `~/Library/Application Support/Anki Agent/`. The desktop app does not access Keychain. A preexisting `.env` can supply initial settings, but once settings have been saved, use the setup screen to change them.

To create a double-clickable development shortcut:

```sh
python create_shortcut.py
```

This creates **Anki Agent.app** in the project folder. It points to this checkout and Python environment; keep both in place or rerun the command after moving them. Startup output is saved in `logs/desktop.log` in the checkout. This shortcut is different from the portable app made by the [Mac packaging workflow](../packaging/README.md).

The optional desktop shortcut defaults to **Control + Option + Space**. Native hotkey registration and disposal must run on the macOS main thread; the launcher queues changes from setup workers onto that thread.

## Browser development

Copy the example configuration:

```sh
cp .env.example .env
```

Edit `.env` with your own values:

| Setting | Value |
| --- | --- |
| `LLM_PROVIDER` | `groq` (default) or `openrouter`. The source browser app also supports `openai`. |
| `OPENROUTER_API_KEY` | Your [OpenRouter key](https://openrouter.ai/settings/keys), required only for `openrouter`. |
| `LLM_MODEL` | Optional override; omit to use the provider default. See [OpenRouter models](OPENROUTER.md). |
| `GROQ_API_KEY` | Your Groq API key from [the Groq console](https://console.groq.com/keys). |
| `DECK_PATH` | The local `collection.anki2` path. On a typical Mac: `~/Library/Application Support/Anki2/User 1/collection.anki2`. Replace `User 1` with the actual profile name. |
| `DECK_NAME` | Exact deck name from Anki; use `::` between parent and child names. Leave blank to include the entire collection. |

Run:

```sh
python run.py
```

Open **http://127.0.0.1:5050** in your browser. The initial launch builds a search index. These `.env` settings apply to the source browser app; users of the downloaded desktop app use Setup instead.

Never commit `.env`, API keys, collections, or personal logs. See [`.env.example`](../.env.example) for optional settings.

## OpenRouter compatibility

Set `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY` for the source browser app. Remove a Groq-specific `LLM_MODEL`, or set a listed OpenRouter model ID. Desktop users save one or both keys in Setup; the model picker includes models from all connected services. See [models and setup](OPENROUTER.md).

The OpenAI-compatible client targets `https://openrouter.ai/api/v1`; it never uses `OPENAI_API_KEY` for OpenRouter. Key testing uses authenticated `GET /key` because `/models` is public. Client routing and request options are selected from each request’s model, without changing the process provider for concurrent requests. All AI paths use the same request adapter, which translates token limits and omits unsupported temperature settings for GPT-6 Luna. Reasoning is requested at low effort. The tool loop preserves OpenRouter `reasoning_details` and Gemini tool signatures between rounds, while showing only readable model-returned text in Thinking. Some models return no readable reasoning.

Regression tests use the actual OpenAI SDK with mock HTTP/SSE responses, including Gemini search → tool result → cited answer, provider/key isolation, and Setup UI races. They make no paid requests; test actual provider availability with your own account before a release.

## Study features

- **Keyword search** runs the local indexed text/tag search without a model call. See the [search guide](SEARCH.md) for the user-facing comparison.
- **AI assist** (`assisted_search`) uses a tool loop to discover tags, browse the tag tree, and probe keyword searches. It returns an AND/OR keyword-and-tag plan, which is executed locally against the selected deck. There is no per-result model screening. The UI endpoint returns matches up to its 100,000-result safety bound.
- **Deep search** (`deep_search`) first builds a broader plan and retrieves up to `DEEP_SCREEN_HARD_MAX` (400) candidates. When no candidates match, it tries relaxed tag-only or keyword-only plans. Above `DEEP_SCREEN_THRESHOLD` (120), it asks for refinement or confirmation before screening. Screening sends short front/back summaries (280-character text budget plus topic tags), in batches of 12 with up to four concurrent calls, and keeps the cards the model selects with brief reasons. It does not read full cards or evaluate the entire collection. Failed screening batches are reported as incomplete results.
- **Tutor** (`/api/tutor`, `tutor_chat`) streams replies that cite source cards. It is read-only: its tools are `search_deck` and `read_note`. Failed responses retry up to three times after the initial attempt, for at most four attempts.
- Tutor, Organize, AI assist, and Deep search expose model-returned reasoning and tool activity in a collapsible **Thinking** panel. If no reasoning is returned, the panel states that. Keyword search does not invoke the model.
- The model picker is available in both desktop and browser views. **Settings → AI model…** focuses it on Mac. The combined menu groups models by provider and remembers the selected model for subsequent requests.
- **Tutor → Settings** (also **Settings → Tutor settings…** on Mac) edits the Tutor system prompt and first-reply instructions through `/api/tutor/settings`. Instructions are stored in `tutor-settings.json` in the app data folder and read for each new Tutor request. The Tutor toolbar's text-size control is stored locally in the browser and scales conversation text and the composer. Organize keeps its own instructions and text size.
- **Move to deck** moves every card of the current results into another deck through [AnkiConnect](https://ankiweb.net/shared/info/2055492159) (Anki must be running). The **Organize** tab (`/api/agent`, `agent_chat`) is the only chat that can prepare changes: `propose_move` and `propose_card_action` (suspend/unsuspend, e.g. "in my GI deck, suspend everything except GERD"). They find cards (browsing the tag tree) and match deck names, and return proposals; nothing changes until the user confirms the preview, which lists every affected card (compact rows that expand to the full card, 50 at a time, via `/api/notes`). Each move's original decks are logged to `move_history.json` in the app data folder (newest 50 kept) for undo. Set `ANKICONNECT_URL` for a non-default address.
- Card images load from `collection.media` beside the collection. For another media location, set `MEDIA_DIR`. Clicking an image opens a larger view.

Organize verifies that Anki's open profile matches the collection selected in Setup. Move and suspend confirmations require a one-use server preview, valid for ten minutes; changed selections require a new preview. Undo records are tied to their original collection. Older records without a collection identity cannot be safely undone through the app. If Anki's response is lost or a change cannot be verified, its recovery record remains in **Recent changes**, marked **Outcome unconfirmed**, even beyond the normal history limit. Check Anki or use Undo before trying the action again.

The collection is read without modification, except for moves and suspend/unsuspend actions the user confirms, which go through AnkiConnect. AI features send questions and relevant card text to the selected provider; OpenRouter routes these requests to its underlying providers. Citations help verify answers; they do not guarantee correctness.

## Refresh the source app's index

Restart after changing or syncing a collection. For the `.env`-configured browser app, force a rebuild with:

```sh
python -m ankiagent.ingest --force
```

A TSV export (`.txt`) can also be used as `DECK_PATH` in the source browser app. The desktop setup screen expects an Anki collection.

## Tests

```sh
DECK_PATH=/unused/collection.anki2 python -m unittest discover -s tests
node --test tests/test_*_ui.cjs
```

The JavaScript checks use Node's built-in test runner. Node is for development checks, not a requirement for end users.

Pushes to `main` and pull requests run these suites and build a smoke-tested Mac installer automatically. CI uses Python 3.12, Node 24, and `packaging/requirements-macos.txt`. See [automated checks and releases](CI_CD.md) for the exact checks and tagged-release workflow. Set the app version in the root `VERSION` file.

## Common source configuration errors

| Problem | Check |
| --- | --- |
| `DECK_PATH is not set` | Create `.env` and set `DECK_PATH`. |
| Deck file not found | Confirm the collection path and profile name. |
| No deck with the configured name | Use the exact deck name, including `::` separators, or leave `DECK_NAME` blank. |
| AI requests fail | Check the key, connection, model availability, and your provider’s account limits or credits. |
| Missing images | Check `collection.media` beside the collection, or set `MEDIA_DIR` and restart. |

## Code map

```text
run.py                  Source browser entry point
launcher.py             macOS desktop launcher and native menus
desktop_entry.py        Packaged desktop entry point and smoke test
create_shortcut.py      Source-backed .app shortcut and icon generation
ankiagent/
  desktop.py            Setup routes and background deck preparation
  settings.py           Per-user preferences and app-managed .env credentials
  hotkeys.py            Native macOS hotkey registration
  app.py                Study UI server and APIs
  ingest.py             Search-index creation
  parse.py              Anki collection and TSV reading
  search.py             Keyword and tag search
  llm.py                AI search and tutor
  llm_client.py         Model/provider clients
  providers.py          Desktop provider metadata and credential names
  anki_connect.py       AnkiConnect client (the only code that writes to Anki)
  organize.py           Move / suspend preview, apply, and the undo log
  templates/            HTML pages
  static/               Styles and scripts
packaging/              Standalone Mac builds and release instructions
```
