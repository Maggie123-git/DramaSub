"""Which AI model powers analysis and translation (the app's "brain").

Transcription always uses OpenAI Whisper (see ``app/services/transcription.py``).
Context analysis, glossary extraction and translation go to Google Gemini by
default. Gemini is reached through its OpenAI-compatible endpoint, so the
existing OpenAI SDK code paths (chat completions, RateLimitError handling)
are reused unchanged.

BYOK ("bring your own key") users supply an OpenAI key and pay for their own
usage, so their requests stay on OpenAI end to end.

Set ``LLM_PROVIDER=openai`` to switch every request back to OpenAI without a
code change.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal

from openai import AsyncOpenAI, OpenAI

from app.core.config import settings
from app.core.openai_key_context import byok_api_key

logger = logging.getLogger(__name__)

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_LLM_MODEL = "gpt-6.1-sol"

Provider = Literal["gemini", "openai"]


@dataclass(frozen=True)
class LLMTarget:
    """Resolved provider, credentials and model for one analysis/translation job."""

    provider: Provider
    api_key: str
    model: str
    base_url: str | None = None

    @property
    def label(self) -> str:
        """Human-readable provider name for progress messages."""
        return "Google Gemini" if self.provider == "gemini" else "OpenAI"

    def sync_client(self) -> OpenAI:
        """Build a sync OpenAI-SDK client pointed at this provider."""
        return OpenAI(api_key=self.api_key, base_url=self.base_url)

    def async_client(self) -> AsyncOpenAI:
        """Build an async OpenAI-SDK client pointed at this provider."""
        return AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)


def _user_openai_key(provided_key: str | None) -> str | None:
    """Return the caller's own (BYOK) OpenAI key, if this request has one.

    Endpoints hand workers whichever key they chose, and workers put it in the
    ``byok_api_key`` context: the user's own key for BYOK requests, the server's
    ``OPENAI_API_KEY`` for everyone else. Only a key that differs from the
    server key belongs to the user.
    """
    key = provided_key or byok_api_key.get()
    if key and key != settings.OPENAI_API_KEY:
        return key
    return None


def resolve_llm_target(api_key: str | None = None) -> LLMTarget:
    """Pick the provider, key and model for an analysis/translation call.

    Priority:
        1. A BYOK user's own OpenAI key -> OpenAI (they pay for their usage).
        2. ``LLM_PROVIDER=gemini`` with ``GEMINI_API_KEY`` set -> Gemini.
        3. Server ``OPENAI_API_KEY`` -> OpenAI. Also used, with a warning, when
           Gemini is selected but no Gemini key is configured, so a missing
           key degrades to the previous behaviour instead of breaking jobs.

    Raises:
        RuntimeError: If no usable key is configured at all.
    """
    user_key = _user_openai_key(api_key)
    if user_key:
        return LLMTarget(provider="openai", api_key=user_key, model=OPENAI_LLM_MODEL)

    if settings.LLM_PROVIDER == "gemini":
        if settings.GEMINI_API_KEY:
            return LLMTarget(
                provider="gemini",
                api_key=settings.GEMINI_API_KEY,
                model=settings.GEMINI_MODEL,
                base_url=GEMINI_OPENAI_BASE_URL,
            )
        logger.warning(
            "LLM_PROVIDER is 'gemini' but GEMINI_API_KEY is not set; "
            "falling back to OpenAI for analysis and translation."
        )

    if not settings.OPENAI_API_KEY:
        raise RuntimeError(
            "No AI key configured. Set GEMINI_API_KEY, or OPENAI_API_KEY with "
            "LLM_PROVIDER=openai."
        )
    return LLMTarget(
        provider="openai", api_key=settings.OPENAI_API_KEY, model=OPENAI_LLM_MODEL
    )


def completion_kwargs(
    model_name: str, max_tokens: int, temperature: float | None = None
) -> dict[str, Any]:
    """Provider-specific keyword arguments for ``chat.completions.create``.

    OpenAI's reasoning models take ``max_completion_tokens``. Gemini's
    OpenAI-compatible endpoint documents ``max_tokens`` and maps
    ``reasoning_effort`` onto its thinking level; thinking tokens count toward
    the output budget, so it is kept low by default for these structured tasks.
    Google strongly recommends leaving Gemini 3 models at their default
    temperature (lower values can cause looping), so ``temperature`` is only
    forwarded to OpenAI.
    """
    if model_name.startswith("gemini"):
        return {
            "max_tokens": max_tokens,
            "reasoning_effort": settings.GEMINI_REASONING_EFFORT,
        }
    kwargs: dict[str, Any] = {"max_completion_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs
