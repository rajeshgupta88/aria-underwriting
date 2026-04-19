"""
LLM provider abstraction for Aria.
Reads config/llm_config.yaml to determine the active provider.
Exposes get_client() and call_llm() so the rest of the codebase
never imports openai or anthropic directly.
"""

import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "llm_config.yaml"

def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_provider() -> str:
    return _load_config()["provider"]

def get_provider_config() -> dict:
    cfg = _load_config()
    return cfg[cfg["provider"]]

def get_client():
    """
    Return an initialised client for the active provider.
    Raises ValueError if the required API key env var is not set.
    """
    provider = get_provider()
    pcfg = get_provider_config()

    if provider == "openai":
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in .env")
        return OpenAI(api_key=api_key, timeout=pcfg["timeout_seconds"])

    elif provider == "anthropic":
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env")
        return anthropic.Anthropic(api_key=api_key)

    else:
        raise ValueError(
            f"Unknown provider '{provider}' in llm_config.yaml. "
            "Must be openai or anthropic."
        )

def call_llm(client, system_prompt: str, user_prompt: str) -> str:
    """
    Unified LLM call — same interface regardless of provider.
    Returns the assistant text response as a plain string.
    """
    provider = get_provider()
    pcfg = get_provider_config()

    if provider == "openai":
        response = client.chat.completions.create(
            model=pcfg["model"],
            temperature=pcfg["temperature"],
            max_tokens=pcfg["max_tokens"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        return response.choices[0].message.content.strip()

    elif provider == "anthropic":
        response = client.messages.create(
            model=pcfg["model"],
            temperature=pcfg["temperature"],
            max_tokens=pcfg["max_tokens"],
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()

def llm_status() -> dict:
    """Status dict for the /health endpoint. Makes no API call."""
    provider = get_provider()
    pcfg = get_provider_config()
    key_env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    return {
        "provider": provider,
        "model": pcfg["model"],
        "api_key_set": bool(os.getenv(key_env)),
        "key_env_var": key_env,
    }
