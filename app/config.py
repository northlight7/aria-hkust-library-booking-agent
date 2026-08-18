"""
config.py — API key and settings management for the Booking Assistant.

Stores configuration in a local JSON file (config.json). Never hard-codes
credentials. The API key is stored locally on the user's machine only.
"""

from __future__ import annotations
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "api_provider": "",        # one of PROVIDERS below
    "api_key": "",             # user's API key
    "api_model": "",           # e.g. "deepseek-chat" or "openai/gpt-4o"
    "voice_enabled": True,     # whether voice mode is on by default
    "first_run_complete": False,
}

# Provider defaults. Single source of truth: the frontend dropdown is built
# from GET /api/providers, which reads this dict. All providers here expose
# an OpenAI-compatible chat completions endpoint.
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-flash",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-v4-flash",
    },
}


def load_config() -> dict:
    """Load config from disk, or return defaults if not found."""
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    # Merge with defaults so new keys appear automatically.
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(config: dict) -> None:
    """Write config to disk."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_provider_info(provider: str) -> dict | None:
    """Return {base_url, default_model} for a provider, or None."""
    return PROVIDERS.get(provider)


def is_configured() -> bool:
    """Return True if an API key has been set and first-run is done."""
    cfg = load_config()
    return bool(cfg.get("api_key")) and cfg.get("first_run_complete", False)
