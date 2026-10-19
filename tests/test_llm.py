"""Tests for choosing the AI provider behind analysis and translation."""

import pytest

from app.core import llm
from app.core.config import settings
from app.core.openai_key_context import byok_api_key

SERVER_OPENAI_KEY = "sk-server-key"


@pytest.fixture(autouse=True)
def _server_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", SERVER_OPENAI_KEY)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "gemini-server-key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-test-flash")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")


def test_standard_user_goes_to_gemini() -> None:
    target = llm.resolve_llm_target()
    assert target.provider == "gemini"
    assert target.api_key == "gemini-server-key"
    assert target.model == "gemini-test-flash"
    assert target.base_url == llm.GEMINI_OPENAI_BASE_URL


def test_server_key_in_worker_context_still_means_gemini() -> None:
    """Workers put the server OpenAI key in context for non-BYOK jobs."""
    token = byok_api_key.set(SERVER_OPENAI_KEY)
    try:
        assert llm.resolve_llm_target().provider == "gemini"
    finally:
        byok_api_key.reset(token)


def test_byok_user_key_stays_on_openai() -> None:
    token = byok_api_key.set("sk-user-own-key")
    try:
        target = llm.resolve_llm_target()
    finally:
        byok_api_key.reset(token)
    assert target.provider == "openai"
    assert target.api_key == "sk-user-own-key"
    assert target.model == llm.OPENAI_LLM_MODEL
    assert target.base_url is None


def test_explicit_user_key_stays_on_openai() -> None:
    target = llm.resolve_llm_target("sk-user-own-key")
    assert target.provider == "openai"
    assert target.api_key == "sk-user-own-key"


def test_missing_gemini_key_falls_back_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    target = llm.resolve_llm_target()
    assert target.provider == "openai"
    assert target.api_key == SERVER_OPENAI_KEY


def test_provider_switch_back_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    target = llm.resolve_llm_target()
    assert target.provider == "openai"
    assert target.model == llm.OPENAI_LLM_MODEL


def test_no_keys_at_all_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with pytest.raises(RuntimeError, match="No AI key configured"):
        llm.resolve_llm_target()


def test_gemini_client_points_at_gemini_endpoint() -> None:
    client = llm.resolve_llm_target().async_client()
    assert str(client.base_url) == llm.GEMINI_OPENAI_BASE_URL


def test_completion_kwargs_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEMINI_REASONING_EFFORT", "low")
    assert llm.completion_kwargs("gpt-6.1-sol", 100, temperature=0.3) == {
        "max_completion_tokens": 100,
        "temperature": 0.3,
    }
    # Gemini keeps its default temperature, per Google's Gemini 3 guidance.
    assert llm.completion_kwargs("gemini-test-flash", 100, temperature=0.3) == {
        "max_tokens": 100,
        "reasoning_effort": "low",
    }
