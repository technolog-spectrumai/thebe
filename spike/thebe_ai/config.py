"""Static startup configuration for the AI spike. Read once, when the extension loads.

SPIKE ONLY. Proposed Thebe wiring (not implemented): an `ai:` section in config.yaml -> the
installer writes APP_DIR/secrets/thebe_ai.json (0600) -> Compose mounts it into the jupyterlab
container as the secret /run/secrets/thebe_ai -> this module reads it. There is no runtime key
editing: change config.yaml, deploy, restart the kernel.

Sources, first match wins:
  1. THEBE_AI_CONFIG=<path to a JSON file> (default /run/secrets/thebe_ai if it exists)
  2. ANTHROPIC_API_KEY / OPENAI_API_KEY environment variables, models from
     THEBE_AI_CLAUDE_MODEL / THEBE_AI_OPENAI_MODEL

JSON file:
  {"default": "claude", "timeout": 60, "max_output_tokens": 2048, "max_retries": 2,
   "providers": {"claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant-..."},
                 "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-..."}}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_SECRET = Path("/run/secrets/thebe_ai")
APIS = ("anthropic", "openai")


class ConfigError(Exception):
    """The configuration cannot be used; the message never contains a key."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str                       # what the user types: %%ai claude
    api: str                        # anthropic | openai
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None     # e.g. a proxy or a compatible endpoint


@dataclass(frozen=True)
class AiConfig:
    providers: dict[str, ProviderConfig]
    default: str
    timeout: float = 60.0           # seconds for one request (the SDKs default to 600)
    max_retries: int = 2            # the SDKs retry 408/409/429/5xx with backoff
    max_output_tokens: int = 2048

    def provider(self, name: str | None) -> ProviderConfig:
        name = name or self.default
        if name not in self.providers:
            known = ", ".join(sorted(self.providers)) or "none"
            raise ConfigError(f"No AI provider named {name!r} is configured (configured: {known}).")
        return self.providers[name]


def mask(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 12 else "set"


def _from_file(path: Path) -> AiConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} cannot be read as JSON ({type(exc).__name__}).") from None
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise ConfigError(f"{path} needs a 'providers' mapping.")
    providers = {}
    for name, entry in data["providers"].items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: provider {name!r} must be a mapping.")
        api, model, key = entry.get("api"), entry.get("model"), entry.get("api_key")
        if api not in APIS:
            raise ConfigError(f"{path}: provider {name!r} needs api: one of {', '.join(APIS)}.")
        if not isinstance(model, str) or not model:
            raise ConfigError(f"{path}: provider {name!r} needs a model.")
        if not isinstance(key, str) or not key.strip():
            continue                   # configured without a key: not offered
        providers[name] = ProviderConfig(name, api, model, key.strip(), entry.get("base_url") or None)
    default = data.get("default") or next(iter(providers), "")
    return AiConfig(providers, default, float(data.get("timeout", 60)), int(data.get("max_retries", 2)),
                    int(data.get("max_output_tokens", 2048)))


def load_config(environ: Mapping[str, str] = os.environ) -> AiConfig:
    path = Path(environ["THEBE_AI_CONFIG"]) if environ.get("THEBE_AI_CONFIG") else DEFAULT_SECRET
    if path.exists():
        return _from_file(path)
    providers = {}
    if environ.get("ANTHROPIC_API_KEY"):
        providers["claude"] = ProviderConfig("claude", "anthropic", environ.get("THEBE_AI_CLAUDE_MODEL", "claude-sonnet-5"),
                                             environ["ANTHROPIC_API_KEY"])
    if environ.get("OPENAI_API_KEY"):
        providers["openai"] = ProviderConfig("openai", "openai", environ.get("THEBE_AI_OPENAI_MODEL", "gpt-5.4-mini"),
                                             environ["OPENAI_API_KEY"])
    return AiConfig(providers, environ.get("THEBE_AI_DEFAULT") or next(iter(providers), ""))
