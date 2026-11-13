# Changelog

User-visible changes are recorded here, newest version first.

## [1.4.0 (preview)](https://github.com/ajy25/anki-agent/releases/tag/v1.4.0) — 2026-11-13

Changes since 1.3.1:

### Added

- **OpenRouter:** GPT-6 Luna, Gemini 3.8 Flash, and Gemini 3.5 Flash Lite, alongside the existing Groq models. Streaming, card citations, and search work with either service.
- **One or both API keys:** separate Groq and OpenRouter fields in Setup, with individual key tests. The model menu groups the choices available through your saved keys and routes each request to the selected model's service.
- **Organize:** move, suspend, or unsuspend cards through AnkiConnect. Review the affected cards and their images before confirming, and use Recent changes to undo an action.
- **Move search results:** move the current result set to another deck with a preview and undo.
- **Tutor settings:** adjustable text size, editable system and first-reply instructions, and a restore-defaults option.
- **Chat history preference:** choose whether closing or reopening the app clears conversations; the global shortcut respects this preference.
- **AnkiConnect guidance:** Organize checks its connection and shows installation instructions and a Check again button when needed.
- **Search guidance:** mode-specific explanations and an expandable comparison in the app, plus a guide explaining AI assist, Deep search, examples, screening limits, and API usage.
- Contributor credits for Andrew Yang, Saajan Patel, and Kevin Zhang in the MIT license and root contributors file.

### Changed

- API keys are now saved in an app-managed, plaintext `.env` file restricted to your Mac account, outside the app bundle. App launches no longer request Keychain access, and updates preserve saved keys.
- The model dropdown is visible in the desktop app. The native AI model menu focuses that dropdown.
- Setup and study documentation now follows medical-student workflows, with provider setup, model costs, search explanations, and troubleshooting.
- Built-in examples use generic deck topics. Names and tags from your own deck still appear normally.
- The website keeps its simple demo/download layout and adds release status, provider choices, search guidance, and contributor credits.

### Fixed

- Clear chat failing after startup because its handler called itself; clearing also cancels pending replies so they cannot restore old messages.
- Tutor replies failing repeatedly when a forced tool call was inappropriate, such as for a greeting. The app retries that validation failure with a relaxed tool choice.
- Organize operations now verify the active Anki profile and require a fresh, single-use preview for the exact reviewed cards. Undo records remain tied to the original collection, and uncertain outcomes remain visible for recovery.
- OpenRouter tool conversations preserve Gemini reasoning/signature metadata between searches and answers. Concurrent requests keep provider credentials and model options separate.

### Upgrade notes

- From the Keychain-based 1.3.1 preview, paste your Groq key into Setup once if prompted. You can also add an OpenRouter key. Existing local `.env` keys are retained.
- The Mac preview remains for Apple silicon and is not notarized by Apple. See the [Mac setup guide](docs/MAC_SETUP.md) for installation.
- Download the [1.4.0 Mac installer](https://github.com/ajy25/anki-agent/releases/download/v1.4.0/Anki-Agent-1.4.0-macOS-arm64-preview.dmg).

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
