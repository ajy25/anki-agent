# Anki Agent

Search your Anki cards and ask a tutor questions with links back to your deck.

## Install on a Mac

**[Read the step-by-step Mac setup guide](docs/MAC_SETUP.md)** — written for people who do not code.

The Mac app includes everything it needs to run. You do not need Python, Terminal, or an `.env` file.

You will need:

- **Anki on this Mac**, with the cards you want to study already downloaded or synced.
- **An internet connection** for the tutor and AI search.
- **Your own Groq API key**, created through [Groq](https://console.groq.com/keys). The guide explains how to do this.

### 1. Download and open

Open the [Downloads / Releases page](https://github.com/ajy25/anki-agent/releases). Under **Assets**, download the **Apple silicon (`arm64`) `.dmg`**. The current preview supports Apple silicon Macs only. Avoid **Source code (zip)** and **Source code (tar.gz)**; those are for developers.

**Download status:** version **1.3.1** has been built locally for **Apple silicon** (`arm64`), but no GitHub release has been published yet. If the Releases page has no downloads, ask the maintainer for `Anki-Agent-1.3.1-macOS-arm64-preview.dmg`.

Double-click the `.dmg`, drag **Anki Agent** onto **Applications**, then open **Anki Agent** from Applications.

The preview has not been notarized by Apple. If macOS blocks it because the developer cannot be verified, follow the guide's [first-open instructions](docs/MAC_SETUP.md#if-macos-blocks-the-first-open).

### 2. Complete the setup screen

1. Click **Create a Groq API key**, sign in, create a key, and copy it.
2. Return to Anki Agent, paste it into **Connect Groq**, and click **Test key**.
3. Under **Choose your Anki profile**, select the profile containing your cards.
4. Under **Pick the deck you want to study**, choose your deck. Choosing a parent deck includes its subdecks.
5. Click **Save & open deck**. Leave the window open while it says **Preparing your deck…**; the tutor opens when it is ready.

For help choosing the correct download, finding your deck, or fixing a setup error, use the **[full Mac setup guide](docs/MAC_SETUP.md)**.

## Use the app

- **Search:** find cards by keyword or tag. Turn on **AI assist** or use **Deep search** for a natural-language search.
- **Tutor:** ask a question, watch the answer appear, and click its citations to inspect the source cards.
- **Thinking:** expand this panel to see model-returned reasoning and deck-search activity when available.
- **Change your key or deck:** click **Setup**, or choose **Settings → API key & Anki deck…** in the Mac menu bar.

Your Anki collection is read without changing it. Your key is saved in **macOS Keychain**. AI requests send your questions and relevant card text to **Groq**. Check AI answers against your cards; they can make mistakes.

## Other instructions

- [Mac setup, updates, and troubleshooting](docs/MAC_SETUP.md)
- [Run or develop the source code](docs/DEVELOPMENT.md)
- [Build and publish Mac downloads — for maintainers](packaging/README.md)
