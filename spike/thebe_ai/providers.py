"""One small interface over the official OpenAI and Anthropic SDKs (SPIKE ONLY).

Each provider turns (system prompt, user prompt) into text, streaming the text to `on_text` as
it arrives. The SDKs are imported lazily, so a notebook without a key never loads them. Retries
(429 and 5xx, with backoff) and timeouts are the SDKs' own; errors become ProviderError with a
one-line message that never contains the key.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .config import AiConfig, ProviderConfig

OnText = Callable[[str], None]


class ProviderError(Exception):
    """A request failed; the message is meant for the notebook user."""


class Provider(Protocol):
    def generate(self, system: str, prompt: str, on_text: OnText | None = None) -> str: ...


def _retries(config: AiConfig) -> str:
    return f"{config.max_retries} {'retry' if config.max_retries == 1 else 'retries'}"


def _explain(exc: Exception, provider: ProviderConfig, config: AiConfig) -> ProviderError:
    """The SDKs' exception classes share names; map them by name to one message each."""
    kind = type(exc).__name__
    who = f"{provider.name} ({provider.model})"
    messages = {
        "AuthenticationError": f"{who}: the API key was rejected. Check the key in the Thebe configuration.",
        "PermissionDeniedError": f"{who}: the key may not use this model.",
        "NotFoundError": f"{who}: the model is not available to this key. Check the model name.",
        "RateLimitError": f"{who}: rate limited (still after {_retries(config)}). Wait a minute and try again.",
        "OverloadedError": f"{who}: the service is overloaded. Try again shortly.",
        "APITimeoutError": f"{who}: no answer within {config.timeout:.0f} s.",
        "APIConnectionError": f"{who}: cannot reach the API (network, proxy or DNS).",
        "BadRequestError": f"{who}: the request was refused: {getattr(exc, 'message', exc)}",
    }
    status = getattr(exc, "status_code", None)
    return ProviderError(messages.get(kind) or f"{who}: {kind}{f' (HTTP {status})' if status else ''}.")


class AnthropicProvider:
    def __init__(self, provider: ProviderConfig, config: AiConfig) -> None:
        import anthropic
        self.provider, self.config, self._sdk = provider, config, anthropic
        self._client = anthropic.Anthropic(api_key=provider.api_key, base_url=provider.base_url,
                                           timeout=config.timeout, max_retries=config.max_retries)

    def generate(self, system: str, prompt: str, on_text: OnText | None = None) -> str:
        parts: list[str] = []
        try:
            with self._client.messages.stream(model=self.provider.model, max_tokens=self.config.max_output_tokens,
                                              system=system, messages=[{"role": "user", "content": prompt}]) as stream:
                for text in stream.text_stream:
                    parts.append(text)
                    if on_text:
                        on_text(text)
        except self._sdk.APIError as exc:
            raise _explain(exc, self.provider, self.config) from None
        return "".join(parts)


class OpenAIProvider:
    def __init__(self, provider: ProviderConfig, config: AiConfig) -> None:
        import openai
        self.provider, self.config, self._sdk = provider, config, openai
        self._client = openai.OpenAI(api_key=provider.api_key, base_url=provider.base_url,
                                     timeout=config.timeout, max_retries=config.max_retries)

    def generate(self, system: str, prompt: str, on_text: OnText | None = None) -> str:
        parts: list[str] = []
        try:
            with self._client.responses.stream(model=self.provider.model, instructions=system, input=prompt,
                                               max_output_tokens=self.config.max_output_tokens) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        parts.append(event.delta)
                        if on_text:
                            on_text(event.delta)
        except self._sdk.APIError as exc:
            raise _explain(exc, self.provider, self.config) from None
        return "".join(parts)


def make_provider(provider: ProviderConfig, config: AiConfig) -> Provider:
    try:
        return {"anthropic": AnthropicProvider, "openai": OpenAIProvider}[provider.api](provider, config)
    except ImportError:
        package = "anthropic" if provider.api == "anthropic" else "openai"
        raise ProviderError(f"The {package} package is not installed in the kernel's environment.") from None
