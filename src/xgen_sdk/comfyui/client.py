"""ComfyUI HTTP 클라이언트와 한 번 실행하기(``run_workflow``).

이벤트 루프를 막지 않도록 httpx **async** 만 쓴다. 사내 프록시 환경변수를 타지
않게 ``trust_env=False``, 다른 곳으로 흘러가지 않게 리다이렉트는 따르지 않는다.

확인된 ComfyUI 0.35.0 동작:
    POST /prompt {prompt, client_id, prompt_id?} → {prompt_id, number, node_errors}
        400 {error:{type,message,details,extra_info}, node_errors:{id:{errors:[...], class_type}}}
    GET  /history/{id}   → {} (아직) | {id:{outputs, status:{status_str, completed, messages}}}
    GET  /view?filename&subfolder&type → 바이트
    POST /interrupt {prompt_id?}  — prompt_id 가 지금 도는 작업일 때만 멈춘다
    POST /queue {delete:[id]}     — 대기 중인 작업을 뺀다
    GET  /system_stats, GET /object_info[/{class}] (없는 class 는 {})
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote, urlsplit

import httpx

from xgen_sdk.comfyui.errors import (
    ComfyUIAuthError,
    ComfyUIConnectionError,
    ComfyUIExecutionError,
    ComfyUIOutputError,
    ComfyUIPromptError,
    ComfyUITimeout,
)
from xgen_sdk.comfyui.workflow import natural_key

logger = logging.getLogger("xgen-sdk.comfyui")

# 폴링 중 연속 실패를 이만큼 참는다(일시적인 끊김으로 긴 생성을 버리지 않게)
POLL_FAILURE_LIMIT = 3
# 취소·시간 초과 뒤 중단 요청에 쓰는 시간
ABANDON_TIMEOUT_S = 5.0
_OBJECT_INFO_CONCURRENCY = 4
_DETAIL_BODY_LIMIT = 2000

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".glb": "model/gltf-binary",
    ".json": "application/json",
    ".txt": "text/plain",
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi"}

# 백그라운드로 넘긴 중단 요청 — 참조를 잡아 두지 않으면 GC 가 거둘 수 있다
_BACKGROUND: set = set()


def _ext(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def content_type_for(filename: str) -> str:
    """파일 이름 확장자로 정한 MIME 형(모르면 application/octet-stream)."""
    return _CONTENT_TYPES.get(_ext(str(filename or "")), "application/octet-stream")


def output_kind(filename: str, list_key: str = "") -> str:
    ext = _ext(str(filename or ""))
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    if ext:
        return "file"
    if list_key == "images":
        return "image"
    if list_key in ("videos", "gifs", "video"):
        return "video"
    return "file"


def _check_base_url(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ComfyUIConnectionError("ComfyUI 주소를 입력해 주세요.")
    text = base_url.strip()
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        has_credentials = bool(parts.username or parts.password)
    except ValueError:
        raise ComfyUIConnectionError("ComfyUI 주소 형식이 올바르지 않습니다.") from None
    if parts.scheme.lower() not in ("http", "https") or not hostname:
        raise ComfyUIConnectionError("ComfyUI 주소는 http 또는 https로 시작해야 합니다.")
    if has_credentials:
        raise ComfyUIConnectionError("ComfyUI 주소에는 계정 정보를 넣을 수 없습니다.")
    if parts.query or parts.fragment:
        raise ComfyUIConnectionError("ComfyUI 주소에는 경로까지만 적어야 합니다.")
    return text.rstrip("/")


def _clip(text: str, limit: int = _DETAIL_BODY_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def summarize_node_errors(body: Any) -> List[Dict[str, Any]]:
    """``POST /prompt`` 400 본문 → ``[{node_id, class_type, input, message}]``."""
    out: List[Dict[str, Any]] = []
    if not isinstance(body, dict):
        return out
    node_errors = body.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id in sorted(node_errors, key=natural_key):
            entry = node_errors[node_id]
            if not isinstance(entry, dict):
                continue
            class_type = entry.get("class_type") if isinstance(entry.get("class_type"), str) else None
            for err in entry.get("errors") or []:
                if not isinstance(err, dict):
                    continue
                extra = err.get("extra_info") if isinstance(err.get("extra_info"), dict) else {}
                input_name = extra.get("input_name")
                message = str(err.get("message") or err.get("type") or "Invalid input")
                details = err.get("details")
                if isinstance(details, str) and details and details not in message:
                    message = f"{message} ({details})"
                out.append({
                    "node_id": str(node_id),
                    "class_type": class_type,
                    "input": input_name if isinstance(input_name, str) else None,
                    "message": _clip(message, 500),
                })
    error = body.get("error")
    if not out and isinstance(error, dict):
        extra = error.get("extra_info") if isinstance(error.get("extra_info"), dict) else {}
        node_id = extra.get("node_id")
        if node_id is not None:
            class_type = extra.get("class_type")
            out.append({
                "node_id": str(node_id),
                "class_type": class_type if isinstance(class_type, str) else None,
                "input": None,
                "message": _clip(str(error.get("message") or error.get("type") or "Invalid prompt"), 500),
            })
    return out


def _prompt_error_from_body(body: Any, status_code: int) -> ComfyUIPromptError:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error_type = error.get("type") if isinstance(error.get("type"), str) else None
    elif isinstance(error, str):
        error_type = error
    else:
        error_type = None
    summary = summarize_node_errors(body)
    if error_type == "missing_node_type":
        extra = error.get("extra_info") if isinstance(error, dict) and isinstance(error.get("extra_info"), dict) else {}
        class_type = extra.get("class_type")
        if isinstance(class_type, str) and class_type:
            message = f"ComfyUI 서버에 {class_type} 노드가 설치되어 있지 않습니다."
        else:
            message = "ComfyUI 서버에 워크플로우가 쓰는 노드가 설치되어 있지 않습니다."
    elif error_type == "prompt_no_outputs":
        message = "워크플로우에 결과를 저장하는 노드가 없습니다."
    else:
        message = "ComfyUI가 워크플로우 입력값을 받아들이지 않았습니다."
    detail = body if isinstance(body, (dict, list)) else f"HTTP {status_code}"
    return ComfyUIPromptError(message, node_errors=summary, error_type=error_type, detail=detail)


def _execution_error_from_status(prompt_id: str, status: Dict[str, Any]) -> ComfyUIExecutionError:
    node_id = node_type = exc_type = exc_message = None
    interrupted = False
    for item in status.get("messages") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2 or not isinstance(item[1], dict):
            continue
        event, payload = item
        if event == "execution_error":
            node_id = payload.get("node_id")
            node_type = payload.get("node_type")
            exc_type = payload.get("exception_type")
            exc_message = payload.get("exception_message")
        elif event == "execution_interrupted":
            interrupted = True
            node_id = payload.get("node_id")
            node_type = payload.get("node_type")
    node_id = str(node_id) if node_id is not None else None
    node_type = str(node_type) if node_type is not None else None
    where = f"{node_type} (node {node_id})" if node_type or node_id else "the workflow"
    if interrupted:
        return ComfyUIExecutionError(
            "ComfyUI에서 작업이 중단되었습니다.",
            node_id=node_id, node_type=node_type, interrupted=True,
            detail=f"Execution was interrupted at {where}.",
        )
    if exc_message is not None or exc_type is not None:
        text = str(exc_message or "").strip()
        return ComfyUIExecutionError(
            f"ComfyUI 실행 중 {node_type or '어느'} 노드에서 오류가 났습니다.",
            node_id=node_id, node_type=node_type,
            detail=_clip(f"{where} failed: {exc_type or 'Error'}: {text}".rstrip(": ")),
        )
    return ComfyUIExecutionError(
        "ComfyUI 실행이 오류로 끝났습니다.",
        detail=f"Prompt {prompt_id} finished with status error.",
    )


class ComfyUIClient:
    """ComfyUI 서버 하나와 말하는 async 클라이언트.

    헤더(인증 값 포함)는 요청에만 싣고 속성·repr·오류 어디에도 남기지 않는다.
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = _check_base_url(base_url)
        clean_headers: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ComfyUIConnectionError("ComfyUI 요청 헤더의 이름과 값은 문자열이어야 합니다.")
            clean_headers[key.strip()] = value
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=clean_headers,
            verify=verify_tls,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"ComfyUIClient(base_url={self.base_url!r})"

    # ── 수명 ──
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ComfyUIClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ── 공통 ──
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ComfyUITimeout(
                "ComfyUI 서버가 제한 시간 안에 응답하지 않았습니다.",
                detail=f"{method} {path}: {type(exc).__name__}",
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ComfyUIConnectionError(
                "ComfyUI 서버에 연결할 수 없습니다.",
                detail=f"{method} {path}: {type(exc).__name__}: {exc}",
            ) from None
        self._raise_for_access(response, method, path)
        return response

    @staticmethod
    def _raise_for_access(response: httpx.Response, method: str, path: str) -> None:
        status = response.status_code
        if status in (401, 403):
            raise ComfyUIAuthError(
                "ComfyUI 서버가 인증을 거부했습니다.", detail=f"{method} {path}: HTTP {status}",
            )
        if 300 <= status < 400:
            raise ComfyUIConnectionError(
                "ComfyUI 서버가 다른 주소로 넘기려 해서 주소를 확인해야 합니다.",
                detail=f"{method} {path}: HTTP {status} redirect not followed",
            )

    @staticmethod
    def _json(response: httpx.Response, method: str, path: str) -> Any:
        if response.status_code >= 400:
            raise ComfyUIConnectionError(
                "ComfyUI 서버가 요청을 처리하지 못했습니다.",
                detail=f"{method} {path}: HTTP {response.status_code}: {_clip(response.text, 500)}",
            )
        try:
            return response.json()
        except ValueError:
            raise ComfyUIConnectionError(
                "ComfyUI 서버의 응답을 읽을 수 없어 주소가 ComfyUI인지 확인해야 합니다.",
                detail=f"{method} {path}: HTTP {response.status_code}: non-JSON body {_clip(response.text, 200)!r}",
            ) from None

    async def _get_json(self, path: str) -> Any:
        response = await self._request("GET", path)
        return self._json(response, "GET", path)

    # ── 조회 ──
    async def system_stats(self) -> Dict[str, Any]:
        """``GET /system_stats`` 원본(``{system:{comfyui_version,...}, devices:[...]}``)."""
        data = await self._get_json("/system_stats")
        if not isinstance(data, dict) or not isinstance(data.get("system"), dict):
            raise ComfyUIConnectionError(
                "ComfyUI 서버의 응답을 읽을 수 없어 주소가 ComfyUI인지 확인해야 합니다.",
                detail="GET /system_stats: unexpected body shape",
            )
        return data

    async def object_info(self, class_types: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """``/object_info`` 원본 모양 ``{class: {input, output_node, ...}}``.

        class_types 를 주면 그 class 만 묻고, 서버에 없는 class 는 결과에서 빠진다.
        None 이면 서버의 전체 목록.
        """
        if class_types is None:
            data = await self._get_json("/object_info")
            if not isinstance(data, dict):
                raise ComfyUIConnectionError(
                    "ComfyUI 서버의 응답을 읽을 수 없어 주소가 ComfyUI인지 확인해야 합니다.",
                    detail="GET /object_info: unexpected body shape",
                )
            return data

        names: List[str] = []
        for item in class_types:
            name = str(item).strip() if item is not None else ""
            if name and name not in names:
                names.append(name)
        if not names:
            return {}

        gate = asyncio.Semaphore(_OBJECT_INFO_CONCURRENCY)

        async def one(name: str):
            path = f"/object_info/{quote(name, safe='')}"
            async with gate:
                response = await self._request("GET", path)
            if response.status_code == 404:
                return name, None
            data = self._json(response, "GET", path)
            entry = data.get(name) if isinstance(data, dict) else None
            return name, entry if isinstance(entry, dict) else None

        results = await asyncio.gather(*(one(name) for name in names))
        return {name: entry for name, entry in results if entry is not None}

    # ── 실행 ──
    async def submit(
        self,
        graph: Dict[str, Any],
        client_id: Optional[str] = None,
        *,
        prompt_id: Optional[str] = None,
    ) -> str:
        """``POST /prompt`` → prompt_id.

        prompt_id(소문자 UUID)를 미리 정해 보내면 요청 도중 취소돼도 그 id 로
        중단을 보낼 수 있다. 서버가 돌려준 id 가 정본이다.

        Raises:
            ComfyUIPromptError: 400 검증 실패(``node_errors`` 요약 포함).
        """
        body: Dict[str, Any] = {"prompt": graph, "client_id": client_id or f"xgen-{uuid.uuid4().hex}"}
        if prompt_id:
            body["prompt_id"] = prompt_id
        response = await self._request("POST", "/prompt", json=body)
        if response.status_code == 400:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if parsed is None:
                raise ComfyUIPromptError(
                    "ComfyUI가 워크플로우 입력값을 받아들이지 않았습니다.",
                    detail=f"POST /prompt: HTTP 400: {_clip(response.text, 500)}",
                )
            raise _prompt_error_from_body(parsed, response.status_code)
        data = self._json(response, "POST", "/prompt")
        new_id = data.get("prompt_id") if isinstance(data, dict) else None
        if not isinstance(new_id, str) or not new_id:
            raise ComfyUIConnectionError(
                "ComfyUI 서버의 응답을 읽을 수 없어 주소가 ComfyUI인지 확인해야 합니다.",
                detail="POST /prompt: response has no prompt_id",
            )
        return new_id

    async def _history_entry(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        data = await self._get_json(f"/history/{quote(prompt_id, safe='')}")
        entry = data.get(prompt_id) if isinstance(data, dict) else None
        return entry if isinstance(entry, dict) else None

    async def wait(self, prompt_id: str, *, timeout_s: float, poll_interval: float = 1.0) -> Dict[str, Any]:
        """history 에 끝난 기록이 생길 때까지 폴링해 그 항목을 돌려준다.

        Raises:
            ComfyUIExecutionError: status 가 error(중단 포함).
            ComfyUITimeout: timeout_s 안에 끝나지 않음.
            ComfyUIConnectionError: 연속 ``POLL_FAILURE_LIMIT`` 번 조회 실패.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_s))
        interval = max(0.05, float(poll_interval))
        failures = 0
        while True:
            remaining = deadline - loop.time()
            entry: Optional[Dict[str, Any]] = None
            try:
                entry = await asyncio.wait_for(self._history_entry(prompt_id), timeout=max(remaining, 1.0))
                failures = 0
            except (ComfyUIConnectionError, ComfyUITimeout, asyncio.TimeoutError) as exc:
                failures += 1
                if failures >= POLL_FAILURE_LIMIT and not isinstance(exc, asyncio.TimeoutError):
                    raise
                logger.debug("comfyui: history poll failed for %s (%s)", prompt_id, type(exc).__name__)
            if entry is not None:
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                status_str = status.get("status_str")
                if status_str == "error":
                    raise _execution_error_from_status(prompt_id, status)
                if status.get("completed") is True or status_str == "success" or (not status and "outputs" in entry):
                    return entry
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ComfyUITimeout(
                    "정해진 시간 안에 ComfyUI 작업이 끝나지 않았습니다.",
                    detail=f"Prompt {prompt_id} did not finish within {timeout_s} seconds.",
                )
            await asyncio.sleep(min(interval, remaining))

    def collect_outputs(
        self,
        history_entry: Dict[str, Any],
        output_node_ids: Sequence[str],
    ) -> List[Dict[str, Any]]:
        """고른 출력 노드들의 결과 파일 목록(동기, 네트워크 없음).

        각 노드 outputs 의 **모든 목록**에서 filename 을 가진 항목을
        ``{node_id, filename, subfolder, type, kind}`` 로 모은다. 순서는
        output_node_ids 순서, 같은 파일은 한 번만.
        """
        outputs = history_entry.get("outputs") if isinstance(history_entry, dict) else None
        if not isinstance(outputs, dict):
            return []
        seen = set()
        out: List[Dict[str, Any]] = []
        for node_id in output_node_ids or []:
            node_out = outputs.get(str(node_id))
            if not isinstance(node_out, dict):
                continue
            for key, items in node_out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename")
                    if not isinstance(filename, str) or not filename:
                        continue
                    subfolder = item.get("subfolder") if isinstance(item.get("subfolder"), str) else ""
                    kind_type = item.get("type") if isinstance(item.get("type"), str) and item.get("type") else "output"
                    ident = (filename, subfolder, kind_type)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    out.append({
                        "node_id": str(node_id),
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": kind_type,
                        "kind": output_kind(filename, key),
                    })
        return out

    async def fetch(self, output: Dict[str, Any], *, max_bytes: int) -> bytes:
        """``GET /view`` 로 파일 하나를 받는다. max_bytes 를 넘으면 받다가 끊는다.

        Raises:
            ComfyUIOutputError: ``output_too_large`` / ``no_output``(서버에 파일 없음).
        """
        filename = output.get("filename") if isinstance(output, dict) else None
        if not isinstance(filename, str) or not filename:
            raise ComfyUIOutputError("받을 결과 파일 이름이 없습니다.", code="no_output", detail="output has no filename")
        params = {
            "filename": filename,
            "subfolder": output.get("subfolder") or "",
            "type": output.get("type") or "output",
        }
        limit = int(max_bytes)
        try:
            async with self._client.stream("GET", "/view", params=params) as response:
                self._raise_for_access(response, "GET", "/view")
                if response.status_code == 404:
                    raise ComfyUIOutputError(
                        "생성된 파일을 ComfyUI에서 찾을 수 없습니다.",
                        code="no_output", detail=f"GET /view: HTTP 404 for {filename}",
                    )
                if response.status_code >= 400:
                    raise ComfyUIConnectionError(
                        "ComfyUI 서버가 요청을 처리하지 못했습니다.",
                        detail=f"GET /view: HTTP {response.status_code} for {filename}",
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise ComfyUIOutputError(
                        "생성된 파일이 받을 수 있는 크기보다 큽니다.",
                        code="output_too_large",
                        detail=f"{filename}: {declared} bytes > limit {limit}",
                    )
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        raise ComfyUIOutputError(
                            "생성된 파일이 받을 수 있는 크기보다 큽니다.",
                            code="output_too_large",
                            detail=f"{filename}: more than limit {limit} bytes",
                        )
                return bytes(buffer)
        except httpx.TimeoutException as exc:
            raise ComfyUITimeout(
                "ComfyUI 서버가 제한 시간 안에 응답하지 않았습니다.",
                detail=f"GET /view: {type(exc).__name__}",
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ComfyUIConnectionError(
                "ComfyUI 서버에 연결할 수 없습니다.", detail=f"GET /view: {type(exc).__name__}: {exc}",
            ) from None

    async def interrupt(self, prompt_id: Optional[str] = None) -> None:
        """``POST /interrupt``. prompt_id 를 주면 그 작업이 돌고 있을 때만 멈춘다.

        prompt_id 없이 부르면 서버에서 **지금 도는 작업 무엇이든** 멈추니 공용
        서버에서는 id 를 주는 것이 맞다.
        """
        body = {"prompt_id": prompt_id} if prompt_id else {}
        response = await self._request("POST", "/interrupt", json=body)
        if response.status_code >= 400:
            self._json(response, "POST", "/interrupt")

    async def delete_queued(self, prompt_ids: Sequence[str]) -> None:
        """``POST /queue {delete:[...]}`` — 아직 시작 안 한 작업을 대기열에서 뺀다."""
        ids = [str(pid) for pid in prompt_ids if pid]
        if not ids:
            return
        response = await self._request("POST", "/queue", json={"delete": ids})
        if response.status_code >= 400:
            self._json(response, "POST", "/queue")


# ─── 한 번 실행하기 ──────────────────────────────────────────────
async def _send_abandon(client: ComfyUIClient, prompt_id: str) -> None:
    # 대기열에서 먼저 빼고(아직 대기 중이면 여기서 끝) 돌고 있으면 멈춘다.
    # 순서를 바꾸면 두 요청 사이에 시작한 작업이 끝까지 돈다.
    try:
        await client.delete_queued([prompt_id])
    except Exception as exc:  # noqa: BLE001 — 중단은 최선 노력
        logger.warning("comfyui: queue delete failed for %s (%s)", prompt_id, type(exc).__name__)
    try:
        await client.interrupt(prompt_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("comfyui: interrupt failed for %s (%s)", prompt_id, type(exc).__name__)


def _retrieve(task: "asyncio.Future[Any]") -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled():
        task.exception()


async def abandon_prompt(client: ComfyUIClient, prompt_id: str, *, timeout_s: float = ABANDON_TIMEOUT_S) -> None:
    """대기열 삭제 + 중단을 짧게, 취소에 휘말리지 않게(shield) 보낸다. 오류는 삼킨다."""
    task = asyncio.ensure_future(asyncio.wait_for(_send_abandon(client, prompt_id), timeout_s))
    _BACKGROUND.add(task)
    task.add_done_callback(_retrieve)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # 또 취소됐다 — 요청은 백그라운드에서 마저 가게 두고 원래 취소를 이어 간다
        pass
    except Exception:  # noqa: BLE001 — 시간 초과 등
        logger.warning("comfyui: abandon for %s did not complete", prompt_id)


async def run_workflow(
    client: ComfyUIClient,
    graph: Dict[str, Any],
    *,
    output_node_ids: Sequence[str],
    timeout_s: float,
    max_files: int = 8,
    max_bytes: int = 50_000_000,
) -> Dict[str, Any]:
    """submit → wait → collect → fetch.

    Returns:
        ``{prompt_id, duration_ms, files:[{node_id, filename, content_type, data}]}``
        (max_files 장까지, 파일마다 max_bytes 까지)

    취소(CancelledError)나 시간 초과면 대기열 삭제와 interrupt 를 짧게 shield 로
    보낸 뒤 다시 raise 한다. 폴링 중 연결이 끊겨도 같은 정리를 시도한다.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    prompt_id = str(uuid.uuid4())
    phase = "submit"
    try:
        prompt_id = await client.submit(graph, prompt_id=prompt_id)
        phase = "wait"
        remaining = max(0.0, float(timeout_s) - (loop.time() - started))
        entry = await client.wait(prompt_id, timeout_s=remaining)
    except asyncio.CancelledError:
        await abandon_prompt(client, prompt_id)
        raise
    except ComfyUITimeout:
        await abandon_prompt(client, prompt_id)
        raise
    except ComfyUIConnectionError:
        if phase == "wait":
            await abandon_prompt(client, prompt_id)
        raise

    outputs = client.collect_outputs(entry, output_node_ids)
    if not outputs:
        raise ComfyUIOutputError(
            "워크플로우가 결과 파일을 만들지 않았습니다.",
            code="no_output",
            detail=f"Prompt {prompt_id} produced no files in output nodes {list(output_node_ids)}.",
        )
    files: List[Dict[str, Any]] = []
    for output in outputs[: max(1, int(max_files))]:
        data = await client.fetch(output, max_bytes=max_bytes)
        files.append({
            "node_id": output["node_id"],
            "filename": output["filename"],
            "content_type": content_type_for(output["filename"]),
            "data": data,
        })
    return {
        "prompt_id": prompt_id,
        "duration_ms": int((loop.time() - started) * 1000),
        "files": files,
    }


__all__ = [
    "ABANDON_TIMEOUT_S",
    "ComfyUIClient",
    "POLL_FAILURE_LIMIT",
    "abandon_prompt",
    "content_type_for",
    "run_workflow",
    "summarize_node_errors",
]
