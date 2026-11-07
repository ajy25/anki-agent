# Changelog

User-visible changes are recorded here, newest version first.

## 1.3.1 (preview) — 2026-11-07

This is the first version included in this changelog.

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
