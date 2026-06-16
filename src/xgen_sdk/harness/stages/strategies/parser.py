"""
Parse strategies — API 응답 파싱

geny-harness s09_parse 차용:
  ResponseParser: raw API 응답 → 구조화된 ParsedResponse
  CompletionSignalDetector: 완료 신호 감지

프로바이더별 응답 포맷이 다름:
- Anthropic: content blocks (text, tool_use, thinking)
- OpenAI: choices[0].message (content, tool_calls)
- Google: candidates[0].content.parts

이걸 Strategy로 분리하면 프로바이더 추가 시 파서만 갈아끼면 됨.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..interfaces import Strategy

logger = logging.getLogger("harness.strategy.parser")


@dataclass
class ParsedToolCall:
    """파싱된 도구 호출"""
    tool_use_id: str
    tool_name: str
    tool_input: dict


@dataclass
class ParsedResponse:
    """파싱된 API 응답"""
    text: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    thinking_blocks: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    is_complete: bool = False
    raw: dict = field(default_factory=dict)


class ResponseParser(Strategy, ABC):
    """API 응답 파싱 인터페이스"""

    @abstractmethod
    def parse(self, api_response: dict) -> ParsedResponse:
        ...


class CompletionSignalDetector(Strategy, ABC):
    """완료 신호 감지 인터페이스"""

    @abstractmethod
    def detect(self, text: str, stop_reason: str) -> bool:
        """응답이 최종 완료인지 판단"""
        ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Anthropic 파서
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AnthropicResponseParser(ResponseParser):

    @property
    def name(self) -> str:
        return "anthropic"

    def parse(self, api_response: dict) -> ParsedResponse:
        result = ParsedResponse(raw=api_response)
        result.stop_reason = api_response.get("stop_reason", "")

        for block in api_response.get("content", []):
            block_type = block.get("type", "")

            if block_type == "text":
                result.text += block.get("text", "")

            elif block_type == "tool_use":
                result.tool_calls.append(ParsedToolCall(
                    tool_use_id=block.get("id", ""),
                    tool_name=block.get("name", ""),
                    tool_input=block.get("input", {}),
                ))

            elif block_type == "thinking":
                result.thinking_blocks.append({
                    "content": block.get("thinking", ""),
                })

        result.is_complete = result.stop_reason == "end_turn" and not result.tool_calls
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenAI 파서
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OpenAIResponseParser(ResponseParser):

    @property
    def name(self) -> str:
        return "openai"

    def parse(self, api_response: dict) -> ParsedResponse:
        result = ParsedResponse(raw=api_response)

        choices = api_response.get("choices", [])
        if not choices:
            return result

        message = choices[0].get("message", {})
        result.text = message.get("content", "") or ""
        result.stop_reason = choices[0].get("finish_reason", "")

        for tc in message.get("tool_calls", []):
            import json
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

            result.tool_calls.append(ParsedToolCall(
                tool_use_id=tc.get("id", ""),
                tool_name=tc.get("function", {}).get("name", ""),
                tool_input=args,
            ))

        result.is_complete = result.stop_reason == "stop" and not result.tool_calls
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  완료 신호 감지
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DefaultCompletionDetector(CompletionSignalDetector):

    @property
    def name(self) -> str:
        return "default"

    def detect(self, text: str, stop_reason: str) -> bool:
        # Anthropic: end_turn, OpenAI: stop
        if stop_reason in ("end_turn", "stop"):
            return True
        return False
