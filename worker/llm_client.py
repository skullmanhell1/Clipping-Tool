"""Pluggable LLM client.

A thin abstraction over supported providers (OpenAI, Anthropic) selected via
``settings.llm_provider``. Downstream modules (selection, metadata, emoji) call
:func:`get_llm_client` and use a single ``complete`` / ``complete_json``
interface, so swapping providers is a config change rather than a code change.

Testability:
    * :class:`MockLLMClient` returns canned responses (no network / API key).
    * :func:`set_llm_client` installs a process-wide override so tests (and the
      pipeline) can inject a client via dependency injection.
    * :func:`llm_available` reports whether real credentials are configured.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, Union

from config import LLMProvider, settings


class LLMError(RuntimeError):
    """Raised when an LLM call fails or no provider is configured."""


class BaseLLMClient(ABC):
    """Common interface all provider clients implement."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """Return a text completion for ``prompt`` (optional ``system`` prompt)."""
        raise NotImplementedError

    def complete_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 1024,
    ) -> Union[dict, list]:
        """Return parsed JSON from the model.

        A short instruction is appended nudging the model to emit JSON only, and
        the response is parsed leniently (code fences and surrounding prose are
        tolerated). Raises :class:`LLMError` if no JSON can be extracted.
        """
        sys = (system or "") + (
            "\nYou must respond with valid JSON only. No prose, no code fences."
        )
        raw = self.complete(prompt, system=sys.strip(), temperature=temperature,
                            max_tokens=max_tokens)
        return parse_json(raw)


class OpenAIClient(BaseLLMClient):
    """OpenAI-backed client using the Chat Completions API."""

    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model

    def complete(self, prompt, system=None, temperature=0.7, max_tokens=1024) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # normalise SDK errors
            raise LLMError(f"OpenAI request failed: {exc}") from exc


class AnthropicClient(BaseLLMClient):
    """Anthropic-backed client using the Messages API."""

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    def complete(self, prompt, system=None, temperature=0.7, max_tokens=1024) -> str:
        try:
            resp = self._client.messages.create(
                model=self._model,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            # Concatenate any text blocks in the response.
            parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            return "".join(parts).strip()
        except Exception as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc


class MockLLMClient(BaseLLMClient):
    """Deterministic client for tests and offline development.

    Provide either a callable ``handler(prompt, system) -> str`` or a list of
    canned responses returned in order (the last one repeats once exhausted).
    """

    def __init__(
        self,
        responses: Optional[list[str]] = None,
        handler: Optional[Callable[[str, Optional[str]], str]] = None,
    ) -> None:
        self._responses = list(responses or [])
        self._handler = handler
        self.calls: list[dict[str, Any]] = []  # recorded for assertions

    def complete(self, prompt, system=None, temperature=0.7, max_tokens=1024) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self._handler is not None:
            return self._handler(prompt, system)
        if self._responses:
            return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return ""


# --- JSON extraction helper -------------------------------------------------

def parse_json(text: str) -> Union[dict, list]:
    """Leniently parse JSON from a model response.

    Handles ```json fenced blocks and surrounding prose by extracting the
    outermost JSON object/array. Raises :class:`LLMError` on failure.
    """
    if not text or not text.strip():
        raise LLMError("Empty LLM response")

    cleaned = text.strip()
    # Strip code fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to slicing the outermost JSON value. Decide object-vs-array by
    # whichever opening bracket appears first, then slice to its matching last
    # closing bracket (so an inner array inside an object is not mistaken for
    # the whole payload).
    obj_start = cleaned.find("{")
    arr_start = cleaned.find("[")
    candidates: list[tuple[int, str]] = []
    if obj_start != -1:
        candidates.append((obj_start, "}"))
    if arr_start != -1:
        candidates.append((arr_start, "]"))
    candidates.sort(key=lambda c: c[0])  # earliest opening bracket wins

    for start, close_c in candidates:
        end = cleaned.rfind(close_c)
        if end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMError(f"Could not parse JSON from LLM response: {text[:200]!r}")


# --- provider selection + dependency injection ------------------------------

_client_override: Optional[BaseLLMClient] = None


def set_llm_client(client: Optional[BaseLLMClient]) -> None:
    """Install (or clear, with ``None``) a process-wide client override.

    Used by tests to inject a :class:`MockLLMClient`.
    """
    global _client_override
    _client_override = client


def llm_available() -> bool:
    """Return whether a usable LLM client can be constructed."""
    if _client_override is not None:
        return True
    if settings.llm_provider is LLMProvider.ANTHROPIC:
        return bool(settings.anthropic_api_key)
    return bool(settings.openai_api_key)


def get_llm_client() -> BaseLLMClient:
    """Return the active LLM client.

    Precedence: an injected override, otherwise the provider selected by
    ``settings.llm_provider``. Raises :class:`LLMError` if no credentials.
    """
    if _client_override is not None:
        return _client_override
    if settings.llm_provider is LLMProvider.ANTHROPIC:
        return AnthropicClient()
    return OpenAIClient()
