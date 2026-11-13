# Set up Anki Agent on your Mac

[Project home](../README.md) · [Models and pricing](../README.md#models-and-pricing)

## 1. Sync your Anki deck

Install **[Anki](https://apps.ankiweb.net/)** on your Mac, open it, and click **Sync**. Wait until your cards and images have finished downloading. If you have a deck file ending in `.apkg`, import it into Anki first.

Anki Agent reads the collection on your Mac. Cards that are only on AnkiWeb or your phone need to be synced to the Mac before you start.

## 2. Install Anki Agent

1. Open [Downloads / Releases](https://github.com/ajy25/anki-agent/releases).
2. Under **Assets**, download the **`.dmg`**. Choose the app installer, rather than the **Source code** archives.
3. Open the `.dmg` and drag **Anki Agent** into **Applications**.
4. Open **Anki Agent** from Applications.

The download supports Macs with Apple silicon. Check ** → About This Mac** for an Apple M-series chip.

**Current public download:** [Anki Agent 1.4.0](https://github.com/ajy25/anki-agent/releases/download/v1.4.0/Anki-Agent-1.4.0-macOS-arm64-preview.dmg). Connect Groq, OpenRouter, or both using the setup steps below.

### If macOS blocks the first open

The preview is not notarized by Apple. If you downloaded it from this repository and macOS says it cannot verify the developer:

1. Dismiss the warning after trying to open the app.
2. Open ** → System Settings → Privacy & Security**.
3. Find the message about Anki Agent and click **Open Anyway**.
4. Confirm **Open**, using your Mac password or Touch ID if asked.

[Apple's instructions](https://support.apple.com/en-us/102445) describe this process. If macOS says the app is damaged or will harm your computer, contact the maintainer. A managed school or work Mac may require your administrator's help.

## 3. Add one or both API keys

An API key is a private code that lets Anki Agent use an AI service through your account. It is separate from your Anki login and Mac password.

Setup has two fields: **Groq API key** and **OpenRouter API key**. You can fill either one or both. Leave the other field blank if you do not want to use that service.

| Service | Get your key | Models it adds |
| --- | --- | --- |
| Groq | [Groq API Keys](https://console.groq.com/keys) | GPT-OSS 120B; Qwen 3.8 27B |
| OpenRouter | [OpenRouter Keys](https://openrouter.ai/settings/keys) | GPT-6 Luna; Gemini 3.8 Flash; Gemini 3.5 Flash Lite |

For each service you want to use:

1. Follow the key link in Setup and sign in or create an account.
2. Create an API key and copy it.
3. Paste it into the matching field. Groq keys start with `gsk_`; OpenRouter keys start with `sk-or-`.
4. Click **Test Groq key** or **Test OpenRouter key**. Look for the message confirming that the service accepted it.

A successful test checks the key. Generating answers also requires available usage allowance or credits in that account. Check the service's limits and billing settings before using it. OpenRouter does not require separate OpenAI or Google keys.

Click **Save & open deck** after choosing your cards to save the keys. Testing alone does not save them. On later visits, a **Key saved** message means you can leave that field blank to keep the key. **Show** reveals only a key you have just typed, not a saved key.

## 4. Choose your cards

Under **Choose your Anki profile**, select the profile containing your cards. A common profile name is **User 1**.

Under **Pick the deck you want to study**, select your deck. Choosing a parent deck includes its subdecks. Choose **All decks in this profile** to search across the whole collection.

Click **Save & open deck** and keep the window open while it prepares your cards. The tutor opens when preparation finishes.

### If your profile is missing

1. Open Anki and check that your cards are present. Finish syncing.
2. Return to Setup and click **Refresh**.
3. If the profile is still missing, click **Browse…**. In the file chooser, press **Command-Shift-G**, enter `~/Library/Application Support/Anki2`, and press Return.
4. Open your profile's folder and select **collection.anki2**.

Choose the collection file rather than `collection.media`, a backup, or an exported deck package. If your collection is elsewhere, use **Browse…** or **Use a collection in a different location**. [Anki's file guide](https://docs.ankiweb.net/files.html) explains the folder layout.

## 5. Ask your first question

Use the **AI model** dropdown at the top of the app. It shows only the services for which you have saved keys. With both keys saved, models are grouped under **Groq** and **OpenRouter**; choosing a model selects its service automatically.

Try a question your deck covers, for example:

> Using my deck, explain the difference between Crohn disease and ulcerative colitis.

Watch the answer appear, then click a citation to read the supporting card. Ask a follow-up question in the same chat. **Thinking** shows deck-search activity and any reasoning text the model provides.

We recommend **GPT-6 Luna on OpenRouter** or **GPT-OSS 120B on Groq** for low-cost everyday studying. [Compare all models and costs](../README.md#models-and-pricing).

## Find cards with Search

Open **Search** and choose one of these options:

- **Neither option selected:** enter a known term such as `sucralfate` for a local keyword search.
- **AI assist:** describe a topic, such as “drugs that protect gastric ulcers.” AI explores your deck's tags and tries keywords, then returns cards matching its search.
- **Deep search:** describe a more specific criterion, such as “gastric ulcer treatments that protect the mucosa rather than suppress acid.” AI finds possible matches, then screens short card excerpts and gives a reason for each card kept.

Deep search can take longer and use more API credits. For more than 120 candidates, it asks you to narrow the request or continue; it reviews at most 400 candidates per search. It can miss cards, so try different wording or a keyword search if results seem incomplete.

Expand **How do the search options differ?** below the search box for a reminder, or read the [search guide](SEARCH.md) for the full workflow. The **Thinking** panel in results shows search activity and any reasoning text returned by the model.

## Everyday use

- **Change keys or decks:** open **Setup**, or **Settings → API key & Anki deck…**. Keep saved keys by leaving their fields blank, then save.
- **Change models:** use the **AI model** dropdown. The choice is remembered and applies to your next request.
- **Start a new conversation:** click **Clear chat**. Tutor settings include the option to clear history when the app closes or opens.
- **After syncing new cards:** quit Anki Agent with **Command-Q** and reopen it to refresh the index.
- **Optional shortcut:** enable **Control + Option + Space** in Setup to show or hide the app. The clear-history preference controls whether reopening starts a fresh conversation.
- **Organize cards (experimental):** install [AnkiConnect](https://ankiweb.net/shared/info/2055492159), keep Anki open, and review every proposed change before confirming. Search and Tutor work without AnkiConnect.

## Update the app

Quit Anki Agent, download the new `.dmg`, and drag the app into Applications. Choose **Replace**, then reopen it. Your keys and settings are kept separately and survive replacement. macOS may ask you to approve the updated preview again.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| A service's models are missing | Open Setup, add or test that service's key, and click **Save & open deck**. Wait for the app to reopen. |
| The key is rejected | Use a key from the correct service. Copy it again without spaces or quotation marks, or create a replacement. |
| Key test works but answers fail | Check your internet connection, account credits or usage limits, and whether the selected model is available. Try another model. |
| Wrong cards or an empty deck list | Check Anki's active profile and synced cards, then select the matching profile and deck in Setup. |
| Collection cannot be read | Finish syncing and quit Anki, then choose `collection.anki2` again. |
| Card images are missing | Let Anki finish syncing media. Keep `collection.media` beside `collection.anki2`, then restart Anki Agent. |
| Setup stays on “Preparing your deck…” | Large collections take longer. If it fails or never finishes, quit and reopen the app, then report the exact message if it repeats. |
| App closes after saving Setup | Update to 1.3.1 or later; that version fixed the earlier shortcut-related crash. Report any remaining crash. |

[Report a problem](https://github.com/ajy25/anki-agent/issues) with your app version, macOS version, and the exact error or steps. Remove keys and personal details from screenshots. If asked for logs, open Finder → **Go → Go to Folder…** and enter `~/Library/Application Support/Anki Agent/logs` to find `desktop.log`.

## Where your data goes

- Your Anki collection stays on your Mac. Search and Tutor read it; Organize makes only the changes you confirm through AnkiConnect.
- Questions, conversation context, and relevant card text are sent to the service for the model you select. OpenRouter routes these requests to its model providers.
- Keys are stored as plain text in `~/Library/Application Support/Anki Agent/.env`, with access restricted to your Mac account. Keep this file private. The app does not use Keychain.
- Settings, the search index, and logs are stored in the same Anki Agent folder.

Use citations to check AI answers against your cards and course materials.
