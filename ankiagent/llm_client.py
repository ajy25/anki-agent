"""Chat clients for Groq, OpenRouter, or OpenAI.

LLM_PROVIDER defaults to groq. LLM_MODEL overrides the provider's default;
each provider reads its own API key. reasoning_kwargs and request_kwargs
adapt the shared tool loop to supported provider/model options.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


_REASONING_HINTS = ("gpt-oss", "o1-", "o1", "o3", "o4-", "gpt-5")

_DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-6-luna",
}

GROQ_MODELS = {
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Tool-capable catalog entries checked 2026-11-13. Prices live in README.md.
OPENROUTER_MODELS = {
    "openai/gpt-6-luna": "GPT-6 Luna",
    "google/gemini-3.8-flash": "Gemini 3.8 Flash",
    "google/gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
}

_OPENROUTER_CLIENT = None
_GROQ_CLIENT = None
_OPENAI_CLIENT = None
_GROQ_ASYNC_CLIENT = None


def get_provider(model: str | None = None) -> str:
    p = (os.environ.get("LLM_PROVIDER") or "groq").strip().lower()
    if p not in _DEFAULT_MODELS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER {p!r}; expected one of "
            f"{sorted(_DEFAULT_MODELS)}"
        )
    if model:
        for provider in enabled_providers():
            if model in {"groq": GROQ_MODELS, "openrouter": OPENROUTER_MODELS}.get(provider, {}):
                return provider
    return p


def enabled_providers() -> list[str]:
    """Desktop explicitly enables providers with saved keys; source uses env keys."""
    configured = os.environ.get("ANKIAGENT_PROVIDERS")
    if configured is not None:
        return [p for p in configured.split(",") if p in _DEFAULT_MODELS]
    primary = get_provider()
    return list(dict.fromkeys([primary] + [p for p, key in (
        ("groq", "GROQ_API_KEY"), ("openrouter", "OPENROUTER_API_KEY")) if os.environ.get(key)]))


def get_model(selected: str | None = None) -> str:
    if selected is not None:
        if not isinstance(selected, str) or selected not in available_models():
            raise ValueError("Unsupported model; choose a model from the model picker.")
        return selected
    explicit = (os.environ.get("LLM_MODEL") or "").strip()
    if explicit:
        return explicit
    return _DEFAULT_MODELS[get_provider()]


def available_models() -> dict[str, str]:
    """Combine connected providers, preserving a source-configured custom model."""
    models = {}
    catalogs = {"groq": GROQ_MODELS, "openrouter": OPENROUTER_MODELS}
    for provider in enabled_providers():
        models.update(catalogs.get(provider, {}))
    # Custom model IDs remain a source-only option.
    if "ANKIAGENT_PROVIDERS" not in os.environ:
        default = get_model()
        models.setdefault(default, default)
    return models


def model_groups() -> dict[str, dict[str, str]]:
    groups = {}
    labels = {"groq": "Groq", "openrouter": "OpenRouter", "openai": "OpenAI"}
    for model, label in available_models().items():
        groups.setdefault(labels[get_provider(model)], {})[model] = label
    return groups


def get_client(model: str | None = None):
    """Route the selected model to its provider without changing global settings."""
    global _GROQ_CLIENT, _OPENAI_CLIENT, _OPENROUTER_CLIENT
    provider = get_provider(model)
    if provider == "groq":
        if _GROQ_CLIENT is None:
            from groq import Groq
            key = os.environ.get("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY is not set")
            # No SDK-level retries: they sleep silently on 429s. llm._create
            # retries visibly and fails fast on long or daily limits.
            _GROQ_CLIENT = Groq(api_key=key, max_retries=0)
        return _GROQ_CLIENT
    if provider == "openrouter":
        if _OPENROUTER_CLIENT is None:
            from openai import OpenAI
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                raise RuntimeError("OPENROUTER_API_KEY is not set")
            _OPENROUTER_CLIENT = OpenAI(api_key=key, base_url=OPENROUTER_BASE_URL,
                                        max_retries=0)
        return _OPENROUTER_CLIENT
    # openai
    if _OPENAI_CLIENT is None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai package not installed; add `openai` to requirements.txt"
            ) from e
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _OPENAI_CLIENT = OpenAI(api_key=key, max_retries=0)
    return _OPENAI_CLIENT


def supports_reasoning_effort(model: str | None = None) -> bool:
    m = (model or get_model()).lower()
    return m == "qwen/qwen3.8-27b" or any(h in m for h in _REASONING_HINTS)


def reasoning_kwargs(model: str | None = None) -> dict:
    """Returns {'reasoning_effort': '...'} if the model accepts it, else {}."""
    if get_provider(model) == "openrouter":
        return {"extra_body": {"reasoning": {
            "effort": (os.environ.get("LLM_REASONING_EFFORT") or "low").strip() or "low",
        }}}
    if not supports_reasoning_effort(model):
        return {}
    val = (os.environ.get("LLM_REASONING_EFFORT") or "low").strip() or "low"
    return {"reasoning_effort": val}


def reasoning_output_kwargs(model: str) -> dict:
    """Ask supported Groq models to return reasoning separately from answers."""
    if get_provider(model) != "groq":
        return {}
    if model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        return {"include_reasoning": True}
    if model == "qwen/qwen3.8-27b":
        return {"reasoning_format": "parsed"}
    return {}


# Module-level constants for places that import a `MODEL` name (preserves the
# old llm.MODEL surface).
PROVIDER = get_provider()
MODEL = get_model()


def reset_clients() -> None:
    global _GROQ_CLIENT, _GROQ_ASYNC_CLIENT, _OPENAI_CLIENT, _OPENROUTER_CLIENT
    _GROQ_CLIENT = _GROQ_ASYNC_CLIENT = _OPENAI_CLIENT = _OPENROUTER_CLIENT = None


def request_kwargs(kwargs: dict) -> dict:
    """Translate shared request options into OpenRouter's supported schema."""
    if get_provider(kwargs.get("model")) != "openrouter":
        return kwargs
    out = dict(kwargs)
    if "max_completion_tokens" in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
    if out.get("model") == "openai/gpt-6-luna":
        out.pop("temperature", None)
    return out
