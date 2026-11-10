"""Tutor instructions stored separately from desktop setup and credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .settings import data_dir

MAX_PROMPT_LENGTH = 20000


def default_prompts() -> dict:
    from .llm import TUTOR_AGENT_SYSTEM, TUTOR_FIRST_TURN_FORMAT
    return {"system_prompt": TUTOR_AGENT_SYSTEM, "first_turn_format": TUTOR_FIRST_TURN_FORMAT}


def read_prompts() -> dict:
    defaults = default_prompts()
    try:
        saved = json.loads((data_dir() / "tutor-settings.json").read_text())
        return validate_prompts(saved)
    except (OSError, ValueError, TypeError):
        return defaults


def validate_prompts(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Send Tutor settings as an object.")
    result = {}
    for key in ("system_prompt", "first_turn_format"):
        text = value.get(key)
        if not isinstance(text, str) or len(text) > MAX_PROMPT_LENGTH:
            raise ValueError("Each instruction field must be text of at most 20,000 characters.")
        result[key] = text.strip()
    if not result["system_prompt"]:
        raise ValueError("Enter a system prompt, or restore the default instructions.")
    return result


def save_prompts(value) -> dict:
    config = validate_prompts(value)
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="tutor-settings-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
        os.replace(temporary, root / "tutor-settings.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return config
