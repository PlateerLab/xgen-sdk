"""
Anthropic LLM Provider — httpx SSE 스트리밍

공식 SDK 없이 httpx로 직접 Messages API 호출.
prompt caching, extended thinking, tool use 지원.
"""

import json
import logging
from typing import Any, AsyncGenerator, Optional

import httpx

from .base import LLMProvider, ProviderEvent, ProviderEventType
from ..errors import ProviderError, RateLimitError, OverloadError

logger = logging.getLogger("harness.provider.anthropic")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API 프로바이더"""

    # v0.24.5 — 공용 base._sanitize_tool_defs 가 이 집합을 참조. Anthropic 은
    # prompt caching 용 cache_control 을 tool 단위로 받을 수 있으므로 기본값 외
    # 추가 허용. 새 Anthropic 확장 키가 생기면 여기만 바꾸면 된다.
    ALLOWED_TOOL_KEYS = LLMProvider.ALLOWED_TOOL_KEYS | {"cache_control"}

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514", base_url: Optional[str] = None):
        self._api_key = api_key
        self._model = model
        from .base import normalize_base_url
        self._base_url = normalize_base_url(base_url or ANTHROPIC_API_URL, api_path="messages")

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return self._model

    def supports_tool_use(self) -> bool:
        return True

    def supports_thinking(self) -> bool:
        return "claude" in self._model.lower()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        stream: bool = True,
        thinking: Optional[dict] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[dict] = None,
    ) -> AsyncGenerator[ProviderEvent, None]:
        # response_format: Anthropic Messages API 는 OpenAI 식 response_format 를
        # 받지 않는다(supports_response_format()=False). 인자는 시그니처 정합/TypeError
        # 방지용으로만 수용하고 무시 — 출력 스키마 강제는 system_prompt soft 지시로 폴백.
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        # thinking 모드에서는 temperature 설정 불가 (Anthropic 제약)
        if thinking and thinking.get("type") == "enabled":
            # v0.26.5 — Anthropic 제약: max_tokens > thinking.budget_tokens 필수.
            # 라이브 검증으로 발견 (HTTP 400: "max_tokens must be greater than
            # thinking.budget_tokens"). 사용자가 max_tokens 작게 두고 thinking 켜면
            # 무조건 400. 자동 보정 — max_tokens 가 budget 보다 작거나 같으면
            # budget + 1024 buffer 로 끌어올려 안전 보장.
            budget = int(thinking.get("budget_tokens", 0) or 0)
            if budget > 0 and body["max_tokens"] <= budget:
                body["max_tokens"] = budget + 1024
            body["thinking"] = thinking
        else:
            body["temperature"] = temperature

        if system:
            body["system"] = system

        if tools:
            # v0.24.5 — 공용 sanitize (base._sanitize_tool_defs) 로 이식. Anthropic 은
            # cache_control 확장 키를 허용하므로 클래스 상수로 ALLOWED_TOOL_KEYS 를 확장.
            # 모든 provider 가 동일 방어선을 공유 — 특정 provider 하드코딩 분기 제거.
            body["tools"] = self._sanitize_tool_defs(tools)
            # v0.11.19 — Anthropic tool_choice: {"type": "auto"|"any"|"tool", "name": "..."}.
            # v0.11.20 — "none" 은 Anthropic 공식 미지원 → tools 자체를 제거해 LLM 이 tool 을 못 쓰게 함.
            if tool_choice:
                if tool_choice == "required":
                    body["tool_choice"] = {"type": "any"}
                elif tool_choice == "auto":
                    body["tool_choice"] = {"type": "auto"}
                elif tool_choice == "none":
                    # Anthropic 은 "none" 옵션이 없어 tools 자체를 드롭 (OpenAI semantics 에 맞춤)
                    body.pop("tools", None)
                    body.pop("tool_choice", None)
                else:
                    body["tool_choice"] = {"type": "tool", "name": tool_choice}

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        # prompt caching 활성화
        if tools or system:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"

        if stream:
            async for event in self._stream_request(body, headers):
                yield event
        else:
            event = await self._batch_request(body, headers)
            yield event

    async def _stream_request(
        self, body: dict, headers: dict
    ) -> AsyncGenerator[ProviderEvent, None]:
        """SSE 스트리밍 요청"""
        # 현재 도구 호출 상태 추적
        current_tool_id = ""
        current_tool_name = ""
        tool_input_json = ""

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream("POST", self._base_url, json=body, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise ProviderError.from_status(response.status_code, error_body.decode())

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")

                    if event_type == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool_id = block.get("id", "")
                            current_tool_name = block.get("name", "")
                            tool_input_json = ""

                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        delta_type = delta.get("type", "")

                        if delta_type == "text_delta":
                            yield ProviderEvent(
                                type=ProviderEventType.TEXT_DELTA,
                                text=delta.get("text", ""),
                            )
                        elif delta_type == "thinking_delta":
                            yield ProviderEvent(
                                type=ProviderEventType.THINKING_DELTA,
                                text=delta.get("thinking", ""),
                            )
                        elif delta_type == "input_json_delta":
                            tool_input_json += delta.get("partial_json", "")

                    elif event_type == "content_block_stop":
                        if current_tool_name:
                            try:
                                parsed_input = json.loads(tool_input_json) if tool_input_json else {}
                            except json.JSONDecodeError:
                                parsed_input = {"raw": tool_input_json}

                            yield ProviderEvent(
                                type=ProviderEventType.TOOL_USE,
                                tool_use_id=current_tool_id,
                                tool_name=current_tool_name,
                                tool_input=parsed_input,
                            )
                            current_tool_id = ""
                            current_tool_name = ""
                            tool_input_json = ""

                    elif event_type == "message_delta":
                        delta = data.get("delta", {})
                        usage = data.get("usage", {})
                        yield ProviderEvent(
                            type=ProviderEventType.STOP,
                            stop_reason=delta.get("stop_reason", ""),
                            output_tokens=usage.get("output_tokens", 0),
                        )

                    elif event_type == "message_start":
                        usage = data.get("message", {}).get("usage", {})
                        if usage:
                            yield ProviderEvent(
                                type=ProviderEventType.USAGE,
                                input_tokens=usage.get("input_tokens", 0),
                                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                            )

    async def _batch_request(self, body: dict, headers: dict) -> ProviderEvent:
        """비스트리밍 요청"""
        body["stream"] = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            response = await client.post(self._base_url, json=body, headers=headers)

            if response.status_code != 200:
                raise ProviderError.from_status(response.status_code, response.text)

            data = response.json()
            text_parts = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            usage = data.get("usage", {})
            return ProviderEvent(
                type=ProviderEventType.STOP,
                text="\n".join(text_parts),
                stop_reason=data.get("stop_reason", ""),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                raw=data,
            )
