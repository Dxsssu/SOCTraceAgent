from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
import yaml


load_dotenv()


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """LLM 配置。

    当前按 DeepSeek 的 OpenAI 兼容接口封装，后续可继续扩展。
    """

    api_key: str
    base_url: str
    model: str
    reasoning_effort: str = "high"
    thinking_enabled: bool = True
    request_timeout_seconds: float = 120.0
    max_retries: int = 0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError("缺少环境变量 DEEPSEEK_API_KEY")

        return cls(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
            reasoning_effort=os.environ.get("DEEPSEEK_REASONING_EFFORT", "high").strip(),
            thinking_enabled=os.environ.get("DEEPSEEK_THINKING_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            request_timeout_seconds=float(
                os.environ.get("DEEPSEEK_REQUEST_TIMEOUT_SECONDS", "120")
            ),
            max_retries=max(0, int(os.environ.get("DEEPSEEK_MAX_RETRIES", "0"))),
        )


class LLMClient:
    """Agent 目录下统一使用的 LLM 接口。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self.client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.request_timeout_seconds,
            max_retries=self.config.max_retries,
        )

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        stream: bool = False,
        reasoning_effort: str | None = None,
        extra_body: dict[str, Any] | None = None,
        additional_messages: list[dict[str, str]] | None = None,
    ) -> str:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        if additional_messages:
            messages.extend(additional_messages)
        messages.append({"role": "user", "content": user_prompt})

        payload_extra_body = dict(extra_body or {})
        if self.config.thinking_enabled and "thinking" not in payload_extra_body:
            payload_extra_body["thinking"] = {"type": "enabled"}

        response = self.client.chat.completions.create(
            model=model or self.config.model,
            messages=messages,
            stream=stream,
            reasoning_effort=reasoning_effort or self.config.reasoning_effort,
            extra_body=payload_extra_body,
        )
        return response.choices[0].message.content or ""


def call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
    additional_messages: list[dict[str, str]] | None = None,
) -> str:
    """简化调用入口，供各 Agent 角色直接使用。"""

    client = LLMClient()
    return client.chat(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        reasoning_effort=reasoning_effort,
        extra_body=extra_body,
        additional_messages=additional_messages,
    )


def parse_yaml_response(response_text: str) -> dict[str, Any] | None:
    """解析 LLM 返回的 YAML。"""

    text = (response_text or "").strip()
    if not text:
        return None

    yaml_text = text
    if "```yaml" in text:
        parts = text.split("```yaml", 1)
        yaml_text = parts[1].split("```", 1)[0].strip()
    elif "```" in text:
        parts = text.split("```", 1)
        yaml_text = parts[1].split("```", 1)[0].strip()

    parsed = yaml.safe_load(yaml_text)
    return parsed if isinstance(parsed, dict) else None


__all__ = ["LLMClient", "LLMConfig", "call_llm", "parse_yaml_response"]
