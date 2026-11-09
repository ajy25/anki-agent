# Changelog

User-visible changes are recorded here, newest version first.

## Unreleased

### Added

- **Move to deck:** move every card of a result set into another Anki deck (for example, all Boards & Beyond GI cards into a personal GI deck). A preview shows how many cards will move and from where before anything changes.
- **Anki Agent tab:** ask in plain English to move cards ("put all Boards and Beyond GI cards in my GI deck") or to suspend/unsuspend them ("in my GI deck, suspend everything except GERD"). It finds the cards, matches your deck, and shows a **Review** button.
- **Card previews:** before you confirm, the preview lists every affected card with its images, in compact rows that expand to the full card. Suspends list the cards kept and the cards suspended.
- The **Tutor** tab is unchanged and stays read-only.
- **Undo:** every move or suspend is logged, and **Recent changes** can reverse it.
- Moves go through the AnkiConnect add-on, so Anki must be open with AnkiConnect installed.

## [1.3.1 (preview)](https://github.com/ajy25/anki-agent/releases/tag/v1.3.1) — 2026-11-07

First public preview and the first version included in this changelog.

### Added

- Standalone Apple silicon Mac installer with a setup screen for a Groq API key, Anki profile, and deck selection. Credentials are stored in macOS Keychain.
- Streaming tutor answers and model selection for GPT-OSS 120B and Qwen 3.8 27B through Groq.
- Up to three automatic retries after an initial failed tutor response.
- Expandable Thinking panels for Tutor, AI assist, and Deep search, showing model-returned reasoning and deck activity when available.
- Card images, an expanded image viewer, and ready-to-paste Anki searches for individual notes and result groups.
- Clear Mac setup documentation and a GitHub Pages site with a recording of the running app.
- Automated tests, verified Mac installer builds, dependency update checks, and tagged release drafts through GitHub Actions.

### Fixed

- A macOS crash after saving Setup with the optional global shortcut enabled. Shortcut registration now runs on the main UI thread.
- Empty or failed tutor responses appearing as normal answers; failed attempts are retried and reported as errors when exhausted.
- Late responses overwriting a new conversation or search after the previous request was canceled.
- Deck counts including duplicate notes across parent decks and subdecks.
- Images being removed from card content during indexing.

### Notes

- The Mac preview is not notarized by Apple. See the [Mac setup guide](docs/MAC_SETUP.md) for first-open instructions.
- Each user supplies their own Groq key and local Anki collection. AI requests send relevant card text to Groq; the collection itself is read without changes.
