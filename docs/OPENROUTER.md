# Use OpenRouter with Anki Agent

OpenRouter gives you access to several AI models through one account. You can use it on its own or alongside Groq.

## Connect your account

1. Open **Setup** in Anki Agent.
2. Follow the link beside **OpenRouter API key**, or open [OpenRouter's Keys page](https://openrouter.ai/settings/keys).
3. Sign in, create a key, and copy it. Check your account's credits and spending limits for paid models.
4. Paste the key into **OpenRouter API key** and click **Test OpenRouter key**.
5. Choose your profile and deck, then click **Save & open deck**.

OpenRouter keys start with `sk-or-`. A Groq key or direct OpenAI key does not work in this field. Testing confirms the key is accepted; answers also require available credits and model access.

OpenRouter and the two-key setup are included in [Anki Agent 1.4.0](https://github.com/ajy25/anki-agent/releases/tag/v1.4.0). If you are using 1.3.1, update the app first.

## Choose a model

The **AI model** menu automatically includes the OpenRouter models once its key is saved. If you also save a Groq key, the same menu shows both services in separate groups. Switch models there without returning to Setup. The selected model is used for Tutor, AI assist, Deep search, and Organize. Plain keyword search does not use an AI service.

We recommend **GPT-6 Luna** as a low-cost starting point on OpenRouter, or **GPT-OSS 120B** if you prefer Groq. See the [combined model pricing table](../README.md#models-and-pricing) for all five models, cost examples, and usage details.

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
