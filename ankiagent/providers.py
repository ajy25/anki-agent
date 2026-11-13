"""Desktop providers and their separate credential names."""
PROVIDERS = {
    "groq": {"label": "Groq", "key_env": "GROQ_API_KEY", "prefix": "gsk_",
             "keys_url": "https://console.groq.com/keys"},
    "openrouter": {"label": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "prefix": "sk-or-",
                   "keys_url": "https://openrouter.ai/settings/keys"},
}


def desktop_provider(value: str | None = None) -> str:
    provider = value or "groq"
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Choose Groq or OpenRouter as your AI provider.")
    return provider
