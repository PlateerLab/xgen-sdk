"""ComfyUI 모듈의 오류 — 두 종류로 나뉜다.

* **규칙 오류** (``WorkflowFormatError``, ``ArgumentError``) — 네트워크 없이 난다.
  ``WorkflowFormatError`` 의 message 는 관리 화면에 그대로 싣는 한국어 한 문장,
  ``ArgumentError`` 의 message 는 모델이 읽고 인자를 고칠 영어 문장이다.
* **서버 오류** (``ComfyUIError`` 계열) — ComfyUI 와 말하다 난다. ``code`` 는
  기계가 가르는 값, ``message`` 는 관리 화면용 한국어 한 문장, ``detail`` 은
  ComfyUI 가 준 원문(모델에게 고칠 거리를 줄 때는 이것을 쓴다).

비밀값(토큰·헤더 값)은 어느 오류에도 싣지 않는다 — 요청 헤더를 오류로 옮기는
코드가 이 모듈에는 없다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# ─── 규칙 오류 ────────────────────────────────────────────────────
class WorkflowFormatError(ValueError):
    """``parse_workflow`` 가 받아들일 수 없는 JSON.

    code: ``invalid_json`` | ``ui_format`` | ``empty`` | ``invalid_node``
    """

    def __init__(self, code: str, message: str, *, node_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id


class ArgumentError(ValueError):
    """도구 인자가 매핑과 맞지 않는다. message 는 모델이 고칠 수 있는 영어 문장."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ─── 서버 오류 ────────────────────────────────────────────────────
class ComfyUIError(Exception):
    """ComfyUI 와 말하다 난 오류의 뿌리."""

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ComfyUIConnectionError(ComfyUIError):
    """닿지 않음, 주소가 틀림, ComfyUI 가 아닌 응답, 알 수 없는 HTTP 오류."""

    def __init__(self, message: str, *, detail: Any = None, code: str = "connection") -> None:
        super().__init__(code, message, detail=detail)


class ComfyUIAuthError(ComfyUIError):
    """401/403."""

    def __init__(self, message: str, *, detail: Any = None, code: str = "auth") -> None:
        super().__init__(code, message, detail=detail)


class ComfyUIPromptError(ComfyUIError):
    """``POST /prompt`` 검증 실패. ``node_errors`` 는 요약 목록.

    각 항목: ``{node_id, class_type, input, message}`` (input 은 없으면 None).
    """

    def __init__(
        self,
        message: str,
        *,
        node_errors: Optional[List[Dict[str, Any]]] = None,
        error_type: Optional[str] = None,
        detail: Any = None,
        code: str = "prompt_invalid",
    ) -> None:
        super().__init__(code, message, detail=detail)
        self.node_errors: List[Dict[str, Any]] = list(node_errors or [])
        self.error_type = error_type


class ComfyUITimeout(ComfyUIError):
    """정해진 시간 안에 응답·완료가 없었다."""

    def __init__(self, message: str, *, detail: Any = None, code: str = "timeout") -> None:
        super().__init__(code, message, detail=detail)


class ComfyUIExecutionError(ComfyUIError):
    """실행은 시작됐지만 history 가 error 로 끝났다(중단 포함)."""

    def __init__(
        self,
        message: str,
        *,
        node_id: Optional[str] = None,
        node_type: Optional[str] = None,
        interrupted: bool = False,
        detail: Any = None,
        code: str = "execution",
    ) -> None:
        super().__init__(code, message, detail=detail)
        self.node_id = node_id
        self.node_type = node_type
        self.interrupted = interrupted


class ComfyUIOutputError(ComfyUIError):
    """결과 파일 문제. code: ``output_too_large`` | ``no_output``."""

    def __init__(self, message: str, *, detail: Any = None, code: str = "no_output") -> None:
        super().__init__(code, message, detail=detail)


__all__ = [
    "ArgumentError",
    "ComfyUIAuthError",
    "ComfyUIConnectionError",
    "ComfyUIError",
    "ComfyUIExecutionError",
    "ComfyUIOutputError",
    "ComfyUIPromptError",
    "ComfyUITimeout",
    "WorkflowFormatError",
]
