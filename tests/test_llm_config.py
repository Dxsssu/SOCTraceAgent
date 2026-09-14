from __future__ import annotations

import os
from types import SimpleNamespace
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


def test_paratera_defaults_are_loaded_from_environment() -> None:
    with patch.dict(os.environ, {"PARATERA_API_KEY": "test-key"}, clear=True):
        config = LLMConfig.from_env()

    assert config.api_key == "test-key"
    assert config.base_url == "https://ai.paratera.com/v1/"
    assert config.model == "DeepSeek-V4.1-Flash"
    assert config.embedding_model == "GLM-Embedding-3"
    assert config.reasoning_effort == "low"
    assert config.thinking_enabled is False
    assert config.request_timeout_seconds == 120.0
    assert config.max_retries == 0


def test_embedding_model_can_be_configured() -> None:
    with patch.dict(os.environ, {
        "PARATERA_API_KEY": "test-key",
        "PARATERA_EMBEDDING_MODEL": "custom-embedding",
    }, clear=True):
        assert LLMConfig.from_env().embedding_model == "custom-embedding"


def test_embeddings_use_shared_client_and_preserve_input_order() -> None:
    config = LLMConfig(api_key="test-key", base_url="https://example.invalid", model="chat-model")
    with patch("src.agent.llm.OpenAI") as openai:
        create = openai.return_value.embeddings.create
        create.return_value.data = [
            SimpleNamespace(index=1, embedding=[0.3, 0.4]),
            SimpleNamespace(index=0, embedding=[0.1, 0.2]),
        ]
        client = LLMClient(config)
        assert client.embed(["告警", "调查"]) == [[0.1, 0.2], [0.3, 0.4]]
        create.assert_called_once_with(model="GLM-Embedding-3", input=["告警", "调查"])
        create.reset_mock()
        assert client.embed([]) == []
        create.assert_not_called()
        client.embed(["证据"], model="override-embedding")
        create.assert_called_once_with(model="override-embedding", input=["证据"])


def test_chat_uses_paratera_compatible_request_shape() -> None:
    config = LLMConfig(
        api_key="test-key",
        base_url="https://ai.paratera.com/v1/",
        model="DeepSeek-V4.1-Flash",
    )

    with patch("src.agent.llm.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value.choices[
            0
        ].message.content = "Hello World"
        client = LLMClient(config)
        result = client.chat(system_prompt="system", user_prompt="Hello World")

    assert result == "Hello World"
    openai.return_value.chat.completions.create.assert_called_once_with(
        model="DeepSeek-V4.1-Flash",
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Hello World"},
        ],
        stream=False,
        reasoning_effort="low",
        extra_body={"thinking": {"type": "disabled"}},
    )
