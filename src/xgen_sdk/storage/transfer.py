"""xgen_sdk.storage.transfer — 큰 파일을 메모리에 올리지 않고 받고 내보내기 (2.14.0+).

클라이언트 업로드는 backend-gateway 의 공통 조각 업로드(``/api/uploads``)를 거쳐 서비스에
1GiB 까지 들어온다(게이트웨이가 원래 요청 형식 그대로 재구성해 스트리밍한다). 서비스는 파일을
``await upload.read()`` 로 통째로 읽지 말고, 이 모듈로 **디스크에 받아 경로로** 다룬다.

받기 (FastAPI/Starlette ``UploadFile``):

    from xgen_sdk.storage.transfer import spooled, upload_max_bytes

    async with spooled(file, upload_max_bytes()) as (path, size):
        upload_file(client, path, object_name, bucket_name=bucket)

    # 이미 연 파일에 옮기며 sha256 도 같이:
    size = await write_chunks(file, out, upload_max_bytes(), digest=hashlib.sha256())

내보내기 (저장소 객체 → 응답, 복호화 포함):

    return await storage_file_response(client, object_name, bucket_name=bucket,
                                       filename="보고서.pdf", media_type="application/pdf")

규칙
- 1MiB 단위로 옮기며 상한을 넘는 순간 413 (``HTTPException``) — 초과분을 계속 읽지 않는다.
- 디스크 쓰기는 워커 스레드에서 한다. 느린 볼륨에서 ``write()`` 한 번이 서빙 루프 전체를 세우지 않게.
- 임시 파일은 ``spooled`` / ``storage_file_response`` 가 지운다(응답은 전송이 끝난 뒤).
- 상한 기본값은 ``XGEN_UPLOAD_MAX_BYTES``(기본 1GiB) — 게이트웨이 ``UPLOAD_MAX_FILE_BYTES`` 와 같은 값.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from typing import Any, AsyncIterator, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException

__all__ = [
    "UPLOAD_MAX_BYTES_ENV",
    "DEFAULT_UPLOAD_MAX_BYTES",
    "upload_max_bytes",
    "declared_size",
    "write_chunks",
    "spool",
    "spooled",
    "content_disposition",
    "temp_file_response",
    "storage_file_response",
]

UPLOAD_MAX_BYTES_ENV = "XGEN_UPLOAD_MAX_BYTES"
DEFAULT_UPLOAD_MAX_BYTES = 1024 ** 3
_CHUNK = 1024 * 1024


def upload_max_bytes() -> int:
    """플랫폼 공통 업로드 상한 (``XGEN_UPLOAD_MAX_BYTES``, 기본 1GiB). 잘못된 값이면 기본값."""
    raw = os.getenv(UPLOAD_MAX_BYTES_ENV)
    try:
        value = int(raw) if raw not in (None, "") else DEFAULT_UPLOAD_MAX_BYTES
    except ValueError:
        return DEFAULT_UPLOAD_MAX_BYTES
    return value if value > 0 else DEFAULT_UPLOAD_MAX_BYTES


def declared_size(upload: Any) -> Optional[int]:
    """Starlette 가 multipart 를 파싱하며 기록한 크기(``UploadFile.size``). 모르면 None."""
    size = getattr(upload, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def _too_large(max_bytes: int, message: str) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=message or f"파일이 너무 큽니다 (최대 {max_bytes // (1024 * 1024)}MB)",
    )


async def write_chunks(
    upload: Any, out: Any, max_bytes: int, *, too_large: str = "", digest: Any = None,
) -> int:
    """이미 연 파일 ``out`` 에 ``upload`` 를 1MiB 씩 옮긴다 → 옮긴 바이트 수.

    ``digest`` (예: ``hashlib.sha256()``)를 주면 옮기며 갱신한다. 상한을 넘으면 413 —
    선언된 크기(``UploadFile.size``)로 먼저 보고, 없거나 틀려도 읽는 중에 끊는다.
    """
    known = declared_size(upload)
    if known is not None and known > max_bytes:
        raise _too_large(max_bytes, too_large)
    size = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise _too_large(max_bytes, too_large)
        if digest is not None:
            digest.update(chunk)
        await asyncio.to_thread(out.write, chunk)
    return size


async def spool(
    upload: Any, max_bytes: Optional[int] = None, *, too_large: str = "", prefix: str = "xgen-upload-",
    directory: Optional[str] = None, digest: Any = None,
) -> Tuple[str, int]:
    """``upload`` 를 임시 파일로 옮긴다 → ``(경로, 크기)``. 지우는 것은 호출자 몫(``spooled`` 권장).

    ``max_bytes`` 를 비우면 :func:`upload_max_bytes`. 실패하면 임시 파일을 남기지 않는다.
    """
    limit = upload_max_bytes() if max_bytes is None else max_bytes
    known = declared_size(upload)
    if known is not None and known > limit:
        raise _too_large(limit, too_large)
    fd, path = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            size = await write_chunks(upload, out, limit, too_large=too_large, digest=digest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path, size


@contextlib.asynccontextmanager
async def spooled(
    upload: Any, max_bytes: Optional[int] = None, *, too_large: str = "", prefix: str = "xgen-upload-",
    directory: Optional[str] = None, digest: Any = None,
) -> AsyncIterator[Tuple[str, int]]:
    """``async with spooled(file) as (path, size):`` — 블록을 나가면 임시 파일을 지운다.

    블록 안에서 파일을 다른 곳으로 옮겼으면(``os.replace``) 지울 것이 없으니 그대로 둔다.
    """
    path, size = await spool(
        upload, max_bytes, too_large=too_large, prefix=prefix, directory=directory, digest=digest,
    )
    try:
        yield path, size
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """한글 파일명도 안전한 Content-Disposition (RFC 5987 ``filename*``). 헤더는 latin-1 이다."""
    kind = "inline" if disposition == "inline" else "attachment"
    return f"{kind}; filename*=UTF-8''{quote(str(filename or 'file'))}"


def temp_file_response(
    path: str,
    *,
    filename: str,
    media_type: str = "application/octet-stream",
    disposition: str = "attachment",
) -> Any:
    """이미 받아 둔 임시 파일을 ``FileResponse`` 로 흘려 보내고, 전송이 끝나면 지운다.

    서비스가 자기 방식(복호화·변환 포함)으로 파일을 디스크에 만든 뒤 응답할 때 쓴다.
    """
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename, disposition)},
        background=BackgroundTask(_unlink_quietly, path),
    )


async def storage_file_response(
    client: Any,
    object_name: str,
    *,
    bucket_name: str,
    filename: str = "",
    media_type: str = "application/octet-stream",
    disposition: str = "attachment",
    prefix: str = "xgen-download-",
) -> Any:
    """저장소 객체를 임시 파일로 받아(복호화 포함) ``FileResponse`` 로 흘려 보낸다.

    ``get_object_bytes_decrypted`` + ``Response(content=...)`` 는 파일 전체를 메모리에 올린다 — 1GiB 파일이면
    파드 하나가 그만큼 더 쓴다. 이 응답은 디스크에서 나눠 보내고(Range 요청도 Starlette 가 처리) 전송이
    끝나면 임시 파일을 지운다. 받기 실패는 그대로 올린다(호출자가 404 등으로 바꾼다).
    """
    from xgen_sdk.storage.minio_client import download_file

    fd, path = tempfile.mkstemp(prefix=prefix)
    os.close(fd)
    try:
        await asyncio.to_thread(download_file, client, object_name, path, bucket_name)
    except BaseException:
        _unlink_quietly(path)
        raise
    return temp_file_response(
        path,
        filename=filename or os.path.basename(object_name),
        media_type=media_type,
        disposition=disposition,
    )


def _unlink_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
