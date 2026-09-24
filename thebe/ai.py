"""The ai: section of config.yaml: AI code generation in notebooks (%%ai and the AI cell button).

The API keys are set here, before a deploy, and nowhere else: there is no screen that edits
them. On install, update, start and restart the builder and run.py write the section as JSON
into a private file next to the installer's settings (.ai.json, mode 600), or delete that file
when AI is off. The installer copies it into APP_DIR/secrets/, where only the AI gateway
container (stack/ai/gateway.py) reads it: notebooks, the dashboard and the browser never see a
key. The gateway also counts the tokens against the budget.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from thebe.settings import has_control, write_private_file

AI_FILE_NAME = ".ai.json"            # next to the installer's settings file; the installer reads it
APIS = ("anthropic", "openai")
PERIODS = ("month", "total")
MAX_PROVIDERS = 8
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_OUTPUT_TOKENS = 2048
TIMEOUT_RANGE = (5, 600)
OUTPUT_TOKENS_RANGE = (16, 200_000)
MAX_BUDGET = 10 ** 12

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_KEY = re.compile(r"[\x21-\x7e]{8,1024}")          # printable ASCII, no spaces

SECTION_KEYS = ("enabled", "default", "providers", "budget", "timeout", "max_output_tokens")
PROVIDER_KEYS = ("api", "model", "api_key", "base_url")
BUDGET_KEYS = ("max_tokens", "period")


@dataclass(frozen=True)
class Provider:
    name: str                               # what %%ai <name> selects
    api: str                                # anthropic or openai
    model: str
    api_key: str = field(repr=False)
    base_url: str = ""                      # '' = the provider's own API


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = True
    default: str = ""                       # a provider name ('' = the first one)
    providers: tuple[Provider, ...] = ()
    max_tokens: int = 0                     # budget per period, input + output; 0 = no limit
    period: str = "month"                   # month: starts again on the 1st (UTC); total: never
    timeout: int = DEFAULT_TIMEOUT          # seconds per answer
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    @property
    def default_provider(self) -> str:
        return self.default or (self.providers[0].name if self.providers else "")

    @property
    def active(self) -> bool:
        """Deployed with the gateway: switched on and at least one provider."""
        return self.enabled and bool(self.providers)


def mask_key(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 12 else "…"


def _int(value: object, where: str, low: int, high: int) -> tuple[int | None, str]:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None, f"{where} must be a whole number from {low:,} to {high:,}."
    return value, ""


def _text(value: object, where: str) -> tuple[str | None, str]:
    if not isinstance(value, str):
        return None, f"{where} must be text; put it in quotes."
    return value, ""


def _base_url_problem(url: str, where: str) -> str:
    if has_control(url) or any(ch.isspace() for ch in url):
        return f"{where} must not contain spaces or control characters."
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{where} is not a valid URL."
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return f"{where} must be an http(s) address such as https://api.example.com (no user name or password)."
    return ""


def _parse_provider(name: object, value: object) -> tuple[Provider | None, list[str]]:
    where = f"ai.providers.{name}"
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return None, [f"ai.providers: the name {str(name)!r} must start with a lowercase letter and use only "
                      "a-z, 0-9, - and _ (at most 32 characters)."]
    if not isinstance(value, dict):
        return None, [f"{where} must be a section with api, model and api_key."]
    problems = [f"unknown setting '{where}.{key}'." for key in value if key not in PROVIDER_KEYS]
    api, model, key = value.get("api"), value.get("model"), value.get("api_key")
    if api not in APIS:
        problems.append(f"{where}.api must be one of: {', '.join(APIS)}.")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        problems.append(f"{where}.model must be a model name such as claude-sonnet-5 or gpt-5.4-mini "
                        "(letters, digits and . _ : / @ + -).")
    if not isinstance(key, str):
        problems.append(f"{where}.api_key must be set, as text in quotes.")
    elif not _KEY.fullmatch(key) or "..." in key or "…" in key:
        # The message never contains the key.
        problems.append(f"{where}.api_key is not a usable key: 8 to 1024 characters, no spaces, not the example's '...'.")
    base_url = value.get("base_url")
    if base_url in (None, ""):
        base_url = ""
    elif not isinstance(base_url, str):
        problems.append(f"{where}.base_url must be text; put it in quotes.")
    else:
        problem = _base_url_problem(base_url, f"{where}.base_url")
        if problem:
            problems.append(problem)
    if problems:
        return None, problems
    return Provider(name, api, model, key, base_url.rstrip("/")), []


def parse_ai(value: object) -> tuple[AiConfig | None, list[str]]:
    """The ai: section, or None when it is absent or has problems (listed; AI then stays off)."""
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ["ai must be a section (indented key: value lines)."]
    problems = [f"unknown setting 'ai.{key}'." for key in value if key not in SECTION_KEYS]
    settings: dict[str, object] = {}

    enabled = value.get("enabled", True)
    if isinstance(enabled, bool):
        settings["enabled"] = enabled
    else:
        problems.append("ai.enabled must be true or false.")

    providers: list[Provider] = []
    raw = value.get("providers")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.append("ai.providers must be a section with one entry per provider (claude:, openai:, ...).")
        raw = {}
    for name, entry in raw.items():
        provider, provider_problems = _parse_provider(name, entry)
        problems += provider_problems
        if provider is not None:
            providers.append(provider)
    if len(raw) > MAX_PROVIDERS:
        problems.append(f"ai.providers lists {len(raw)} providers; at most {MAX_PROVIDERS} are allowed.")
    settings["providers"] = tuple(providers)
    if settings.get("enabled", True) and not raw:
        problems.append("ai.providers is empty: add a provider with its api, model and api_key, "
                        "or set ai.enabled: false.")

    default = value.get("default")
    if default not in (None, ""):
        text, problem = _text(default, "ai.default")
        if problem:
            problems.append(problem)
        elif text not in raw:
            problems.append(f"ai.default names {text!r}, which is not one of ai.providers.")
        else:
            settings["default"] = text

    budget = value.get("budget")
    if budget is not None:
        if not isinstance(budget, dict):
            problems.append("ai.budget must be a section with max_tokens and period.")
        else:
            problems += [f"unknown setting 'ai.budget.{key}'." for key in budget if key not in BUDGET_KEYS]
            if budget.get("max_tokens") is not None:
                number, problem = _int(budget["max_tokens"], "ai.budget.max_tokens", 0, MAX_BUDGET)
                problems += [problem] if problem else []
                if number is not None:
                    settings["max_tokens"] = number
            if budget.get("period") is not None:
                if budget["period"] in PERIODS:
                    settings["period"] = budget["period"]
                else:
                    problems.append(f"ai.budget.period must be one of: {', '.join(PERIODS)}.")

    for key, (low, high) in (("timeout", TIMEOUT_RANGE), ("max_output_tokens", OUTPUT_TOKENS_RANGE)):
        if value.get(key) is not None:
            number, problem = _int(value[key], f"ai.{key}", low, high)
            problems += [problem] if problem else []
            if number is not None:
                settings[key] = number

    if problems:
        return None, problems
    return AiConfig(**settings), []


def render_ai(ai: AiConfig | None, scalar: Callable[[str], str]) -> list[str]:
    """config.yaml lines for the ai: section (a commented example when there is none)."""
    head = [
        "# AI code generation in notebooks: the %%ai magic and the AI button in the cell toolbar.",
        "# The keys are read from this file on install and update (Deploy in the builder) only. They",
        "# reach the AI gateway container, never notebooks or the browser. The budget counts the input",
        "# and output tokens of all providers together; the Statistics page shows how much is left.",
    ]
    if ai is None:
        example = (
            ("ai:", ""),
            ('  default: "claude"', "what a plain %%ai uses; the others by name: %%ai openai"),
            ("  providers:", ""),
            ("    claude:", ""),
            ('      api: "anthropic"', ""),
            ('      model: "claude-sonnet-5"', ""),
            ('      api_key: "sk-ant-..."', ""),
            ("    openai:", ""),
            ('      api: "openai"', ""),
            ('      model: "gpt-5.4-mini"', ""),
            ('      api_key: "sk-..."', ""),
            ("  budget:", ""),
            ("    max_tokens: 1000000", "0 = no limit"),
            ('    period: "month"', "month: starts again on the 1st (UTC); total: never"),
            (f"  timeout: {DEFAULT_TIMEOUT}", "seconds to wait for one answer"),
            (f"  max_output_tokens: {DEFAULT_MAX_OUTPUT_TOKENS}", "the longest answer, in tokens"),
        )
        return head + [f"# {line:<32} # {note}" if note else f"# {line}" for line, note in example]
    lines = head + [
        "ai:",
        f"  enabled: {'true' if ai.enabled else 'false'}",
        "  # What a plain %%ai uses; the others by name: %%ai openai",
        f"  default: {scalar(ai.default_provider)}",
        "  providers:" if ai.providers else "  providers: {}",
    ]
    for provider in ai.providers:
        lines += [
            f"    {provider.name}:",
            f"      api: {scalar(provider.api)}",
            f"      model: {scalar(provider.model)}",
            f"      api_key: {scalar(provider.api_key)}",
        ]
        if provider.base_url:
            lines.append(f"      base_url: {scalar(provider.base_url)}")
    lines += [
        "  budget:",
        "    # Tokens per period, input and output of all providers together; 0 = no limit.",
        f"    max_tokens: {ai.max_tokens}",
        "    # month: the count starts again on the 1st of each month (UTC); total: it never does.",
        f"    period: {scalar(ai.period)}",
        "  # Seconds to wait for one answer, and the longest answer in tokens.",
        f"  timeout: {ai.timeout}",
        f"  max_output_tokens: {ai.max_output_tokens}",
    ]
    return lines


def gateway_settings(ai: AiConfig) -> dict:
    """What the AI gateway reads (/run/secrets/ai_config): the section with its keys."""
    return {
        "default": ai.default_provider,
        "providers": {p.name: {"api": p.api, "model": p.model, "api_key": p.api_key, "base_url": p.base_url or None}
                      for p in ai.providers},
        "max_tokens": ai.max_tokens,
        "period": ai.period,
        "timeout": ai.timeout,
        "max_output_tokens": ai.max_output_tokens,
    }


def ai_file(settings_file: Path) -> Path:
    """The installer's AI settings: .ai.json next to its settings file (the installer's rule too)."""
    return settings_file.with_name(AI_FILE_NAME)


def save_ai_file(path: Path, ai: AiConfig | None) -> bool:
    """Write the gateway's settings (mode 600), or delete the file when AI is off. True when on.

    Raises OSError when the file cannot be written or removed.
    """
    if ai is None or not ai.active:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    write_private_file(path, json.dumps(gateway_settings(ai), indent=1, ensure_ascii=False) + "\n")
    return True


def describe(ai: AiConfig | None) -> str:
    """One line for run.sh check and the builder's log: never a key, only its last characters."""
    if ai is None:
        return "off (no ai: section)"
    if not ai.enabled:
        return "off (ai.enabled: false)"
    names = ", ".join(f"{p.name} = {p.api} {p.model}, key {mask_key(p.api_key)}" for p in ai.providers)
    budget = (f"{ai.max_tokens:,} tokens {'per month' if ai.period == 'month' else 'in total (never reset)'}"
              if ai.max_tokens else "no token limit")
    return f"on, default {ai.default_provider} ({names}); {budget}; timeout {ai.timeout} s"
