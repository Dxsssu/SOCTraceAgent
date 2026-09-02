from __future__ import annotations

from unittest.mock import patch

from src.agent.llm import LLMClient, LLMConfig


def test_llm_client_applies_bounded_request_settings() -> None:
    config = LLMConfig(
        api_key="test-key",
        base_url="https://example.invalid",
        model="test-model",
        request_timeout_seconds=45.0,
        max_retries=0,
    )

    with patch("src.agent.llm.OpenAI") as openai:
        LLMClient(config)

    openai.assert_called_once_with(
        api_key="test-key",
        base_url="https://example.invalid",
        timeout=45.0,
        max_retries=0,
    )
