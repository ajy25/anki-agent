# Use OpenRouter with Anki Agent

OpenRouter gives you access to several AI models through one account. You can use it on its own or alongside Groq.

## Connect your account

1. Open **Setup** in Anki Agent.
2. Follow the link beside **OpenRouter API key**, or open [OpenRouter's Keys page](https://openrouter.ai/settings/keys).
3. Sign in, create a key, and copy it. Check your account's credits and spending limits for paid models.
4. Paste the key into **OpenRouter API key** and click **Test OpenRouter key**.
5. Choose your profile and deck, then click **Save & open deck**.

OpenRouter keys start with `sk-or-`. A Groq key or direct OpenAI key does not work in this field. Testing confirms the key is accepted; answers also require available credits and model access.

The published 1.3.1 download supports Groq only. OpenRouter and the two-key setup are part of the upcoming **1.4.0**, available in the current source and updated local builds.

## Choose a model

The **AI model** menu automatically includes the OpenRouter models once its key is saved. If you also save a Groq key, the same menu shows both services in separate groups. Switch models there without returning to Setup. The selected model is used for Tutor, AI assist, Deep search, and Organize. Plain keyword search does not use an AI service.

| Model | Input per million tokens | Output per million tokens | Starting point |
| --- | ---: | ---: | --- |
| [GPT-6 Luna](https://openrouter.ai/openai/gpt-6-luna) | $0.10 | $0.50 | Lowest listed rates; default when only OpenRouter is connected. |
| [Gemini 3.8 Flash](https://openrouter.ai/google/gemini-3.8-flash) | $0.825 | $4.125 | Newest tool-capable Gemini Flash in the checked catalog. |
| [Gemini 3.5 Flash Lite](https://openrouter.ai/google/gemini-3.5-flash-lite) | $0.30 | $2.50 | A less expensive Gemini option. |

Prices are USD from OpenRouter's [model catalog](https://openrouter.ai/api/v1/models), checked November 13, 2026. Follow the model links for current pricing. GPT-6 Luna has higher rates for very long contexts, and Gemini 3.8 Flash's model page may show promotional rates that differ from the catalog.

**What counts toward cost?** A token is a small piece of text. Input includes your question, conversation history, instructions, and retrieved cards. Output includes the answer and billable reasoning. One question may use several calls to find and read cards, so these are not fixed prices per question. View actual charges and credits in the [OpenRouter platform](https://openrouter.ai/).

## What to expect

Answers stream as they are generated and can cite cards from your deck. All three models support the tool calls the app uses to search and read cards. The **Thinking** panel shows search activity and readable reasoning when returned by the model; some responses contain no readable reasoning.

Your Groq and OpenRouter keys are saved separately. Leave either saved key's field blank in Setup to keep it. Keys survive app updates.

If the key test passes but an answer fails, check credits, spending limits, model availability, and account provider restrictions. Try another model. For other issues, see the [Mac setup guide](MAC_SETUP.md).

## Your data

OpenRouter receives your question, conversation context, and relevant card text, then routes the request to the providers serving your selected model. Your account's routing and data policies apply. The app does not enforce zero data retention. Keys are kept in the app's local `.env` file, restricted to your Mac account.

## Source configuration

For the browser app run from source:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-your_key_here
# Optional: add a Groq key to show its models too.
# GROQ_API_KEY=gsk_your_key_here
# Optional default model:
# LLM_MODEL=google/gemini-3.8-flash
```

Set `DECK_PATH` and optionally `DECK_NAME` as described in [development setup](DEVELOPMENT.md). Keep your `.env` out of Git.
