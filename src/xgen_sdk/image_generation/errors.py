"""이미지 생성 공통 오류.

``ImageGenerationError.code`` 는 기계가 가르는 값(로그 ``error_code`` 에 그대로),
``message`` 는 사용자 · 관리 화면에 싣는 한국어 한 문장(제공자 이름 없이
"이미지 생성 서비스"로 말한다), ``detail`` 은 제공자가 준 원문 일부(모델에게
고칠 거리를 줄 때 쓴다; 키 · 헤더 값은 싣지 않는다).
"""
from __future__ import annotations

from typing import Any, Optional

CODE_CONNECTION = "connection"
CODE_AUTH = "auth"
CODE_INVALID_INPUT = "invalid_input"
CODE_SAFETY_BLOCKED = "safety_blocked"
CODE_TIMEOUT = "timeout"
CODE_NO_OUTPUT = "no_output"
CODE_QUOTA = "quota"

ERROR_CODES = (
    CODE_CONNECTION,
    CODE_AUTH,
    CODE_INVALID_INPUT,
    CODE_SAFETY_BLOCKED,
    CODE_TIMEOUT,
    CODE_NO_OUTPUT,
    CODE_QUOTA,
)

USER_MESSAGES = {
    CODE_CONNECTION: "이미지 생성 서비스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    CODE_AUTH: "이미지 생성 서비스 인증에 실패했습니다. API 키 설정을 확인해 주세요.",
    CODE_INVALID_INPUT: "이미지 생성 요청이 올바르지 않습니다. 입력 값을 확인해 주세요.",
    CODE_SAFETY_BLOCKED: "안전 정책에 따라 이미지를 만들 수 없습니다. 요청 내용을 바꿔 다시 시도해 주세요.",
    CODE_TIMEOUT: "이미지 생성 서비스가 제한 시간 안에 응답하지 않았습니다.",
    CODE_NO_OUTPUT: "이미지 생성 서비스가 이미지를 돌려주지 않았습니다.",
    CODE_QUOTA: "이미지 생성 서비스의 사용 한도를 넘었습니다. 잠시 후 다시 시도하거나 관리자에게 문의해 주세요.",
}

# 자주 쓰는 구체 문구(code 는 위 7개 중 하나)
MSG_NO_KEY = "이미지 생성 서비스의 API 키가 설정되지 않았습니다. 관리자에게 문의해 주세요."
MSG_EDIT_UNSUPPORTED = "이 모델은 참조 이미지 편집을 지원하지 않습니다."
MSG_TOO_MANY_REFERENCES = "참조 이미지가 너무 많습니다."
MSG_EMPTY_PROMPT = "만들 이미지 설명(프롬프트)을 입력해 주세요."
MSG_UNKNOWN_PROVIDER = "알 수 없는 이미지 생성 제공자입니다."


class ImageGenerationError(Exception):
    """이미지 생성 · 모델 조회에서 난 오류(코드 7종)."""

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        *,
        detail: Any = None,
        status_code: Optional[int] = None,
        provider: Optional[str] = None,
    ) -> None:
        if code not in ERROR_CODES:
            code = CODE_CONNECTION
        msg = message or USER_MESSAGES[code]
        super().__init__(msg)
        self.code = code
        self.message = msg
        self.detail = detail
        self.status_code = status_code
        self.provider = provider

    @property
    def retryable(self) -> bool:
        return self.code in (CODE_CONNECTION, CODE_TIMEOUT, CODE_QUOTA)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return f"ImageGenerationError(code={self.code!r}, message={self.message!r})"


def user_message(code: str) -> str:
    return USER_MESSAGES.get(code, USER_MESSAGES[CODE_CONNECTION])


def from_comfyui_error(exc: Exception) -> ImageGenerationError:
    """``xgen_sdk.comfyui`` 오류 → 공통 오류. 원래 code 는 detail 에 남긴다."""
    from xgen_sdk.comfyui.errors import (
        ArgumentError,
        ComfyUIAuthError,
        ComfyUIConnectionError,
        ComfyUIError,
        ComfyUIExecutionError,
        ComfyUIOutputError,
        ComfyUIPromptError,
        ComfyUITimeout,
    )

    if isinstance(exc, ImageGenerationError):
        return exc
    if isinstance(exc, ArgumentError):
        return ImageGenerationError(CODE_INVALID_INPUT, detail=exc.message, provider="comfyui")
    if isinstance(exc, ComfyUIError):
        detail = {"comfyui_code": exc.code, "detail": exc.detail}
        if isinstance(exc, ComfyUIAuthError):
            code = CODE_AUTH
        elif isinstance(exc, ComfyUITimeout):
            code = CODE_TIMEOUT
        elif isinstance(exc, ComfyUIPromptError):
            code = CODE_INVALID_INPUT
        elif isinstance(exc, ComfyUIOutputError):
            code = CODE_NO_OUTPUT
        elif isinstance(exc, ComfyUIExecutionError):
            # 실행 실패는 서버 쪽 사정 — 사용자 문구는 comfyui 가 준 중립 문구를 쓴다
            return ImageGenerationError(CODE_CONNECTION, exc.message, detail=detail, provider="comfyui")
        elif isinstance(exc, ComfyUIConnectionError):
            code = CODE_CONNECTION
        else:
            code = CODE_CONNECTION
        return ImageGenerationError(code, exc.message, detail=detail, provider="comfyui")
    return ImageGenerationError(CODE_CONNECTION, detail=type(exc).__name__, provider="comfyui")


__all__ = [
    "CODE_AUTH",
    "CODE_CONNECTION",
    "CODE_INVALID_INPUT",
    "CODE_NO_OUTPUT",
    "CODE_QUOTA",
    "CODE_SAFETY_BLOCKED",
    "CODE_TIMEOUT",
    "ERROR_CODES",
    "ImageGenerationError",
    "MSG_EDIT_UNSUPPORTED",
    "MSG_EMPTY_PROMPT",
    "MSG_NO_KEY",
    "MSG_TOO_MANY_REFERENCES",
    "MSG_UNKNOWN_PROVIDER",
    "USER_MESSAGES",
    "from_comfyui_error",
    "user_message",
]
