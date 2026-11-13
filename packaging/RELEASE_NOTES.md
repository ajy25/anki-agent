# Anki Agent 1.4.0 — Mac preview

Download the **.dmg**, open it, and drag **Anki Agent** into **Applications**. Open the app, add a Groq key, an OpenRouter key, or both, then select your Anki profile and deck.

## What’s new

- OpenRouter models: **GPT-6 Luna**, **Gemini 3.8 Flash**, and **Gemini 3.5 Flash Lite**. The model dropdown includes choices from whichever services you connect.
- **Experimental Organize:** manage cards with preview, confirmation, and undo through AnkiConnect, including moving an entire search result set.
- Tutor text size and instruction settings, reliable Clear chat, and an option to clear chat history when the app closes or opens.
- Clearer AI assist and Deep search explanations in the app and documentation.
- Keys saved in a local, account-restricted `.env` file, without Keychain prompts.
- Fixes for failed tutor tool calls and protections that bind Organize actions to the active profile and reviewed cards.

For low-cost everyday studying, start with **GPT-6 Luna on OpenRouter** or **GPT-OSS 120B on Groq**. [Compare all model prices](https://github.com/ajy25/anki-agent#models-and-pricing).

## Updating from 1.3.1

Quit the app before replacing it. If Setup asks for your Groq key, paste it once and save; the app no longer reads Keychain. Existing app-managed `.env` keys and deck preferences are retained. Add an OpenRouter key if you want its models as well.

## Mac compatibility

Apple silicon (`arm64`). Local builds and live model requests were checked on **macOS 26.6.2**. This preview has an ad-hoc signature and is not notarized by Apple; follow the [Mac setup guide](https://github.com/ajy25/anki-agent/blob/main/docs/MAC_SETUP.md) if macOS blocks the first open.

The ZIP is an alternative app download. The JSON manifest records the source revision, architecture, smoke checks, and installer checksums.

[Setup guide](https://github.com/ajy25/anki-agent/blob/main/docs/MAC_SETUP.md) · [Search guide](https://github.com/ajy25/anki-agent/blob/main/docs/SEARCH.md) · [Full changelog](https://github.com/ajy25/anki-agent/blob/main/CHANGELOG.md)

Contributors: Andrew Yang, Saajan Patel, and Kevin Zhang.
