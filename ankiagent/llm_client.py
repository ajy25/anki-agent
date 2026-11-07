"""Pluggable LLM client (Groq or OpenAI).

Configured via .env:
  LLM_PROVIDER          groq | openai          (default: groq)
  LLM_MODEL             model id               (default: openai/gpt-oss-120b
                                                 for groq, gpt-4o-mini for openai)
  LLM_REASONING_EFFORT  low | medium | high    (default: low; ignored for
                                                 models that don't support it)
  GROQ_API_KEY          required if provider=groq
  OPENAI_API_KEY        required if provider=openai

Both Groq and OpenAI expose the same `client.chat.completions.create(...)`
surface, so call sites stay provider-agnostic. The one quirk is the
`reasoning_effort` kwarg — only some models accept it. Use
`reasoning_kwargs()` to splat it in conditionally.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


_REASONING_HINTS = ("gpt-oss", "o1-", "o1", "o3", "o4-", "gpt-5")

_DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "openai": "gpt-4o-mini",
}

GROQ_MODELS = {
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B",
}

_GROQ_CLIENT = None
_OPENAI_CLIENT = None
_GROQ_ASYNC_CLIENT = None


def get_provider() -> str:
    p = (os.environ.get("LLM_PROVIDER") or "groq").strip().lower()
    if p not in _DEFAULT_MODELS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER {p!r}; expected one of "
            f"{sorted(_DEFAULT_MODELS)}"
        )
    return p


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
    """Choices for the current provider, preserving a configured custom model."""
    models = dict(GROQ_MODELS) if get_provider() == "groq" else {}
    default = get_model()
    models.setdefault(default, default)
    return models


def get_client():
    """Return a chat-completions client for the configured provider."""
    global _GROQ_CLIENT, _OPENAI_CLIENT
    provider = get_provider()
    if provider == "groq":
        if _GROQ_CLIENT is None:
            from groq import Groq
            key = os.environ.get("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY is not set")
            _GROQ_CLIENT = Groq(api_key=key)
        return _GROQ_CLIENT
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
        _OPENAI_CLIENT = OpenAI(api_key=key)
    return _OPENAI_CLIENT


def supports_reasoning_effort(model: str | None = None) -> bool:
    m = (model or get_model()).lower()
    return m == "qwen/qwen3.8-27b" or any(h in m for h in _REASONING_HINTS)


def reasoning_kwargs(model: str | None = None) -> dict:
    """Returns {'reasoning_effort': '...'} if the model accepts it, else {}."""
    if not supports_reasoning_effort(model):
        return {}
    val = (os.environ.get("LLM_REASONING_EFFORT") or "low").strip() or "low"
    return {"reasoning_effort": val}


def reasoning_output_kwargs(model: str) -> dict:
    """Ask supported Groq models to return reasoning separately from answers."""
    if get_provider() != "groq":
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
