"""Pluggable LLM client.

A thin abstraction over supported providers (OpenAI, Anthropic) selected via
``settings.llm_provider``. Downstream modules (selection, metadata, emoji) call
:func:`get_llm_client` and use a single ``complete`` interface, so swapping
providers is a config change rather than a code change.

STUB ONLY.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from config import LLMProvider, settings


class BaseLLMClient(ABC):
    """Common interface all provider clients implement."""

    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> str:
        """Return a completion for ``prompt``."""
        raise NotImplementedError


class OpenAIClient(BaseLLMClient):
    """OpenAI-backed client. TODO(phase-llm): implement using the SDK."""

    def complete(self, prompt: str, **kwargs) -> str:  # noqa: D102
        raise NotImplementedError


class AnthropicClient(BaseLLMClient):
    """Anthropic-backed client. TODO(phase-llm): implement using the SDK."""

    def complete(self, prompt: str, **kwargs) -> str:  # noqa: D102
        raise NotImplementedError


def get_llm_client() -> BaseLLMClient:
    """Return the configured LLM client based on ``settings.llm_provider``."""
    if settings.llm_provider is LLMProvider.ANTHROPIC:
        return AnthropicClient()
    return OpenAIClient()
