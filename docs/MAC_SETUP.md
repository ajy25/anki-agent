# Set up Anki Agent on your Mac

This guide takes you from downloading the app to asking your first question. You do not need to code, use Terminal, install Python, or edit a settings file.

[Back to the project home page](../README.md)

## Before you start

1. Install the desktop version of **[Anki](https://apps.ankiweb.net/)** if it is not already on your Mac.
2. Open Anki. Make sure you can see the deck you want to study. If your cards are on another device, click **Sync** and wait for it to finish. If you have a deck download ending in `.apkg`, import it into Anki first.
3. Keep your Mac connected to the internet for Groq setup and AI features.

Anki Agent uses the cards stored on **this Mac**. Signing in to AnkiWeb in a browser, or having cards only on your phone, does not put those cards on the Mac.

## 1. Choose the right download

To check which Mac you have, click ** → About This Mac** in the top-left corner of your screen. [Apple's guide](https://support.apple.com/en-us/116943) explains where to look.

| What you see | Download to choose |
| --- | --- |
| **Chip: Apple M1, M2, M3**, or another Apple M-series chip | A Mac `.dmg` with **`arm64`** in its name. This means Apple silicon. |
| **Processor: Intel…** | **No Intel installer is available.** The current preview cannot run on an Intel Mac. |

Open the **[Anki Agent Downloads / Releases page](https://github.com/ajy25/anki-agent/releases)**. Select the newest release for your Mac and expand **Assets** if needed. Click the matching `.dmg` file.

**Current availability:** an Apple silicon preview, **1.3.1**, has been built but is not yet published on GitHub. If you see no releases or no matching `.dmg`, ask the maintainer for the installer. The current file is `Anki-Agent-1.3.1-macOS-arm64-preview.dmg`. There is no Intel download available here yet; Intel compatibility has not been verified.

The release notes should state which macOS versions have been tested. Do not assume every older Mac is supported.

**Which file should I click?**

- **`.dmg`** — the recommended Mac installer. Use this one.
- **`.zip` containing Anki Agent.app** — an alternative app download; double-click to unpack it, then drag the app into Applications.
- **Source code (zip)** or **Source code (tar.gz)** — project files for developers. These do not install the app.
- **`.json`** — technical information about the download. You do not need to open it.

## 2. Put the app in Applications

1. Open **Finder → Downloads** and double-click the downloaded `.dmg`.
2. A window opens showing **Anki Agent.app** and **Applications**. Drag **Anki Agent.app** onto **Applications**. Wait for copying to finish.
3. Close that window. In Finder, open **Applications** and double-click **Anki Agent**. Open this installed copy each time.
4. After installation, you can eject **Anki Agent** under **Locations** in Finder. The downloaded `.dmg` can be deleted after the app is working.

### If macOS blocks the first open

The current preview has not been notarized by Apple, so macOS may say it cannot verify the developer or check the app. If you downloaded it from this repository's maintainer and choose to trust it:

1. Dismiss the warning after trying to open **Anki Agent**.
2. Open ** → System Settings → Privacy & Security**.
3. Scroll to **Security**. Find the message about **Anki Agent** and click **Open Anyway**.
4. Confirm **Open**. Enter your **Mac login password** or use Touch ID if asked.

On older macOS versions, these settings may be called **System Preferences → Security & Privacy**. [Apple's first-open instructions](https://support.apple.com/en-us/102445) explain the prompts.

This approval is for this app. You do not need to turn off your Mac's security protections or run a Terminal command. If there is no **Open Anyway** option, or the message says the app **will damage your computer** or **is damaged**, stop and contact the maintainer. A school- or work-managed Mac may need help from its administrator.

**You are ready for the next step when you see “Set up Anki Agent.”**

## 3. Add your Groq API key

An API key is a private access code that lets Anki Agent use Groq's AI service. It is separate from your Anki account and your Mac password. Each person should use their own key.

1. In Anki Agent, under **Connect Groq**, click **Create a Groq API key ↗**. You can also open **[Groq's API Keys page](https://console.groq.com/keys)** in your browser.
2. Sign in to Groq, or create an account if you do not have one.
3. Choose **Create API Key**. If asked for a name, use something recognizable, such as **Anki Agent**. Copy the generated key.
4. Return to Anki Agent. Click the field under **Connect Groq** and paste with **Command-V**. The key starts with `gsk_`. Paste the key itself, without quotation marks.
5. Click **Test key**. You should see **“Connected. Your key was accepted by Groq.”**

The field hides the key as you type. **Show** lets you check what you pasted. It is saved in **macOS Keychain** when you click **Save & open deck** in step 5. Testing alone does not save it.

Keep your key private. Do not put it in a GitHub issue, screenshot, or message asking for help. Groq usage follows your own account's limits and billing settings; check those in the [Groq console](https://console.groq.com/) rather than assuming unlimited free use. See [Groq's key setup documentation](https://console.groq.com/docs/quickstart) for account help.

## 4. Choose your Anki profile and deck

**Choose your Anki profile:** select the profile containing your cards. A profile is Anki's separate card collection for a person or purpose; a common default name is **User 1**. If you have only one profile, it may already be selected.

**Pick the deck you want to study:** choose your deck from the list. For example, choose **AnKing Step Deck** to include its subdecks, or choose one subdeck to focus on that subject. Choose **All decks in this profile** to search across the whole profile.

You do not need to export your cards, type a deck name, or move Anki's files.

### If your profile does not appear

1. Open Anki and confirm your cards are present on this Mac. Finish syncing if needed.
2. Return to Anki Agent and click **Refresh** beside the profile list.
3. If it is still missing, click **Browse…**. In the file chooser, press **Command-Shift-G**, paste the folder below, and press **Return**:

   ```text
   ~/Library/Application Support/Anki2
   ```

4. Open the folder named after your Anki profile, such as **User 1**. Select **collection.anki2** and click **Open**.
5. Choose your deck from the dropdown in Anki Agent.

**collection.anki2** is Anki's local card collection. Select that file, rather than the `collection.media` folder, a backup, or an exported `.apkg` / `.colpkg`. For an exported package, import it into Anki first. If you keep Anki in a custom folder, use **Browse…** to find that profile's `collection.anki2` instead. [Anki's file-location guide](https://docs.ankiweb.net/files.html) has more detail.

## 5. Save and try it

1. Leave **Enable the global shortcut** unchecked for a simple first setup. You can turn it on later if you want **Control + Option + Space** to show or hide the app.
2. Click **Save & open deck**.
3. Leave the window open while it says **Preparing your deck…**. Large decks take longer. When preparation finishes, the app opens the tutor automatically.
4. Try a question about a topic in your cards, such as **“Explain the difference between Crohn disease and ulcerative colitis using my deck.”** Choose a topic your deck actually covers.
5. Watch the answer appear. Click a citation to inspect a source card. You can expand **Thinking** to see model-returned reasoning and search activity when available.

Setup is complete when you can ask a question and inspect its source cards. AI answers can be wrong; check the cited cards when studying.

## Come back later or change settings

- Open **Finder → Applications → Anki Agent**. Your saved key and deck choice are remembered. You can drag the installed app into the Dock for easier access.
- To change your key, profile, deck, or optional shortcut, click **Setup** in the app, or **Settings → API key & Anki deck…** in the Mac menu bar. Leave the key field blank to keep the saved key. Click **Save & open deck** after making changes.
- After adding or syncing cards in Anki, fully quit Anki Agent with **Command-Q**, then reopen it to refresh its search index.
- To select a different AI model, use **Settings → AI model** in the Mac menu bar. You can leave the default model selected.

## Update the app

1. Quit Anki Agent with **Command-Q**.
2. Download the newer `.dmg` for the same type of Mac.
3. Open it and drag **Anki Agent.app** into **Applications**. Choose **Replace** if asked.
4. Open **Anki Agent** from Applications. You may see the first-open security prompt again.

Your settings are stored separately from the app, so replacing the app preserves them. If macOS asks for Keychain access after an update, check that it names the Anki Agent copy you installed. If your saved key cannot be read, reopen Setup and paste your own key again.

Version **1.3.1** fixes a crash that could happen after saving Setup with the optional shortcut enabled. If you have that problem on an older copy, update before trying again.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| The Downloads / Releases page is empty | The installer has not been published yet. Ask the maintainer for the `.dmg`; downloading the source code will not install the app. |
| The app is incompatible with your Mac | Check ** → About This Mac** and the download's `arm64` / `x86_64` name. Ask for a build tested on your Mac and macOS version. |
| Groq did not accept the key | Copy your own Groq API key again, without spaces or quotation marks. If needed, create a new key, paste it into Setup, and click **Test key**. |
| Could not reach Groq | Check your internet connection and try **Test key** again. If other websites work, try again later or check [Groq's service status](https://groqstatus.com/). |
| No profiles, an empty deck list, or the wrong cards | Open Anki and check the profile and deck. Sync, then return to Setup, click **Refresh**, and choose that profile and deck. Use **Browse…** if needed. |
| Could not read the collection | Finish syncing and quit Anki, then choose its `collection.anki2` again. If it still fails, contact the maintainer with the exact message. |
| Still preparing the deck | Keep the app open. If it never finishes or shows an error, note the message, fully quit, and reopen. Report the message if the problem repeats. |
| AI requests fail after setup | Check your connection and test the saved key in Setup. Groq account limits or service issues may also cause failures. Tutor retries automatically; if it still fails, wait and try again. |
| Card images are missing | Open Anki and let media syncing finish. Keep `collection.media` beside `collection.anki2`, then quit and reopen Anki Agent. |
| The app quits when saving Setup | Use version **1.3.1** or later. If it still happens, report your macOS version, app version, and what you clicked. |

For help, open an **[issue on GitHub](https://github.com/ajy25/anki-agent/issues)** or contact the maintainer. Include your app download's filename, macOS version, exact error message, and the step that failed. Remove API keys and personal card text from screenshots.

If the maintainer asks for logs: in Finder, choose **Go → Go to Folder…**, paste `~/Library/Application Support/Anki Agent/logs`, and open `desktop.log`. Review it before sharing; it may include local file paths. If that folder is absent, say so.

## Where your data goes

- **Anki cards:** read from the collection on your Mac. Anki Agent does not edit or sync the collection.
- **Groq key:** saved in macOS Keychain, separate from the app download.
- **Settings, search index, and logs:** stored in `~/Library/Application Support/Anki Agent/`.
- **AI requests:** your questions and relevant card text are sent to Groq to produce answers and AI searches. Anki Agent is not an entirely offline AI app.
