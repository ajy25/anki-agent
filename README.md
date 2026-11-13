# Anki Agent

[![CI](https://github.com/ajy25/anki-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ajy25/anki-agent/actions/workflows/ci.yml)

Find relevant cards, ask questions about what you are studying, and follow the tutor's citations back to your Anki deck.

**[Watch the demo and download the app](https://ajy25.github.io/anki-agent/)** · **[Mac setup guide](docs/MAC_SETUP.md)**

## Get started

You need **Anki on your Mac**, a synced deck, an internet connection, and an API key from **[Groq](https://console.groq.com/keys)** or **[OpenRouter](https://openrouter.ai/settings/keys)**. An API key connects the app to an AI service using your own account.

1. **Install Anki Agent.** Download the `.dmg` from [Releases](https://github.com/ajy25/anki-agent/releases), open it, and drag **Anki Agent** into **Applications**. The Mac download supports Apple silicon.
2. **Connect an AI service.** In Setup, paste a key into **Groq API key**, **OpenRouter API key**, or both. Click the corresponding **Test key** button. You only need one key to start.
3. **Choose your cards.** Select your Anki profile and deck. Choosing a parent deck includes its subdecks.
4. **Save & open deck.** Leave the window open while the app prepares your cards.
5. **Choose a model and ask a question.** The **AI model** menu shows models from the services you have connected. With both keys saved, you can switch between Groq and OpenRouter directly in this menu.

**Download:** [Anki Agent 1.4.0 for Mac](https://github.com/ajy25/anki-agent/releases/download/v1.4.0/Anki-Agent-1.4.0-macOS-arm64-preview.dmg). This preview includes Groq and OpenRouter, the two-key setup, and the features described below.

If macOS blocks the app or your deck is missing, follow the [Mac setup guide](docs/MAC_SETUP.md).

## Study with your deck

- **Search:** use keywords when you know a term. **AI assist** turns a description into a keyword-and-tag search. **Deep search** adds an AI review of short excerpts from the cards it finds, keeping those judged relevant. [See how the search options work](docs/SEARCH.md).
- **Tutor:** ask a question about your cards, then ask follow-up questions in the same conversation. Replies appear as they are generated. Click a citation such as **[1]** to inspect the supporting card.
- **Thinking:** expand this panel to see search activity and any reasoning text returned by the model. Availability varies by model.
- **Organize (experimental):** request a change such as “move my cardiology cards to my Cardiology deck.” Review the affected cards and confirm before anything changes. Moves and suspend/unsuspend actions require Anki to be open with [AnkiConnect](https://ankiweb.net/shared/info/2055492159) installed. Changes can be undone.
- **Clear chat:** use **Clear chat** in Tutor or Organize. Tutor settings also let you clear chat history when the app closes or opens.
- **Adjust your experience:** change text size or tutor instructions in Tutor settings. Return to **Setup** to add or replace a key or choose another deck. Leave a saved key's field blank to keep it.

## Which search option should I use?

| Option | Example request | What it does |
| --- | --- | --- |
| Keyword search — both AI options off | `sucralfate` | Matches words and tags locally, without an AI call. |
| AI assist | “Find cards about drugs that protect gastric ulcers.” | Explores deck tags and tries keywords, then returns cards matching the search it builds. |
| Deep search | “Gastric ulcer treatments that protect the mucosa rather than suppress acid.” | Finds possible matches, screens short card excerpts for relevance, and gives a reason for each card kept. |

Deep search uses more AI calls and may take longer. It asks you to narrow searches with more than 120 candidates and reviews at most 400 candidates per search. It does not scan every full card in the deck. [Read the search guide](docs/SEARCH.md) for the steps, examples, and how to interpret results.

## Models and pricing

We recommend **GPT-6 Luna on OpenRouter** or **GPT-OSS 120B on Groq** for low-cost everyday studying. Luna has the lowest listed token prices; GPT-OSS 120B combines low prices with Groq’s fast inference. Both are defaults for their respective services.

Anki Agent is free, and the recommended models can cost fractions of a cent for modest requests. For example, **10,000 input tokens plus 1,000 output tokens in total** cost about **$0.0015 (0.15¢) with Luna** or **$0.0021 (0.21¢) with GPT-OSS 120B**. These are illustrative token totals, not measured costs per question.

| Model | Service | Input / 1M tokens | Output / 1M tokens |
| --- | --- | ---: | ---: |
| [GPT-6 Luna](https://openrouter.ai/openai/gpt-6-luna) **Recommended** | OpenRouter | $0.10 | $0.50 |
| [GPT-OSS 120B](https://console.groq.com/docs/model/openai/gpt-oss-120b) **Recommended** | Groq | $0.15 | $0.60 |
| [Qwen 3.8 27B](https://console.groq.com/docs/model/qwen/qwen3.8-27b) | Groq | $0.80 | $4.00 |
| [Gemini 3.8 Flash](https://openrouter.ai/google/gemini-3.8-flash) | OpenRouter | $0.75 | $3.75 |
| [Gemini 3.5 Flash Lite](https://openrouter.ai/google/gemini-3.5-flash-lite) | OpenRouter | $0.30 | $2.50 |

Prices are USD per **1 million tokens**, checked **November 13, 2026**, from the linked providers’ model pages. Input is text sent to the model; output includes generated answers and billable reasoning. The table uses uncached text rates for standard service. Gemini 3.8 Flash currently has a 50% promotion ($1.50/$7.50 before the discount); routing, long-context rates, fees, and future price changes can affect your bill.

One study question can make several model calls. Longer chats, retries, and Deep search increase usage. Keyword search makes no AI calls. Check your provider dashboard for actual charges and set a spending limit that suits you.

Connect either service or both. The model dropdown shows only the models available through your saved keys. [Set up Anki Agent](docs/MAC_SETUP.md).

## Your cards and data

Search and Tutor read your deck without changing it. Organize changes cards only after you confirm a preview, through AnkiConnect. AI requests send your question, conversation context, and relevant card text to the service for your selected model; OpenRouter forwards requests to its model providers. Check answers against the cited cards and your course materials.

API keys are saved in a local `.env` file in `~/Library/Application Support/Anki Agent/`, restricted to your Mac account. They are stored separately from the app and retained when you update it.

## Project information

- [Contributors](CONTRIBUTORS.md)
- [Mac setup and troubleshooting](docs/MAC_SETUP.md)
- [Search options and examples](docs/SEARCH.md)
- [Model comparison and pricing](#models-and-pricing)
- [Changelog](CHANGELOG.md)
- [Report a problem](https://github.com/ajy25/anki-agent/issues)
- [Development instructions](docs/DEVELOPMENT.md)
- [Automated checks and releases](docs/CI_CD.md)
- [Mac packaging](packaging/README.md)
- [Website maintenance](docs/WEBSITE.md)
