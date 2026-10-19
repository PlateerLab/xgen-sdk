"""xgen_sdk.storage.folder_zip: 폴더 하나(하위 폴더 포함)를 zip 으로 묶어 흘려 보낸다 (2.16.0+).

[파일 저장소](documents) 폴더 [다운로드] 와 에이전트 [스토리지](workflow) 폴더 [다운로드] 가 함께 쓴다.
운영체제의 "압축하기" 와 같은 모양이다: zip 안의 맨 위가 그 폴더이고, 하위 폴더 구조와 빈 폴더가 그대로
들어간다. 서비스는 자기 저장소에서 폴더·파일 목록을 읽어 넘기고, 바이트를 임시 파일로 내려 주는 함수만 준다.

    from fastapi.responses import StreamingResponse
    from xgen_sdk.storage.folder_zip import plan_tree, stream
    from xgen_sdk.storage.transfer import content_disposition

    zplan = plan_tree(root_id, "보고서", folders, files)          # 상한을 넘으면 413 (아무것도 보내지 않는다)
    return StreamingResponse(stream(zplan, download), media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(zplan.download_name)})

    folders = [{"id": ..., "parent_id": ..., "name": ...}, ...]           # 맨 위 폴더 자신은 빼도 된다
    files   = [{"folder_id": ..., "name": ..., "size": ..., "item": row}, ...]
    def download(item, path): ...   # item(=files[i]["item"]) 의 원본을 path 에 쓴다(복호화 포함)

지키는 것
---------
- **메모리에 다 올리지 않는다.** 파일 하나씩 임시 파일로 내려 zip 에 넣으며 만든 만큼 바로 보낸다. 첫 바이트가
  곧바로 나가므로 큰 폴더도 응답이 멈춘 듯 보이지 않는다. 임시 파일로 먼저 내리는 것은 zip 머리에 적을 크기가
  넣는 동안 바뀌지 않게 하려는 것이기도 하다(에이전트가 그 파일을 쓰는 중일 수 있다).
- **상한은 보내기 전에 본다.** 파일 수와 합계 크기를 넘으면 zip 을 만들기 전에 413 으로 답한다
  (보내기 시작한 뒤에는 상태 코드를 바꿀 수 없다).
- **보내는 도중 한 파일을 읽지 못하면** 그 파일은 빼고 계속 묶은 뒤, zip 맨 위 폴더에 ``받지 못한 파일.txt``
  로 경로와 사유를 적는다. 말없이 빠지지 않는다.
- **이름.** 경로 구분자·제어 문자는 ``_`` 로 바꾸고(압축을 푸는 쪽이 폴더 밖에 쓰지 않게), 한 폴더에서 이름이
  겹치면(대소문자만 다른 것 포함) ``이름 (2).확장자`` 로 나눈다. 폴더가 파일보다 먼저 이름을 갖는다.
- 이미 압축된 형식(그림·영상·압축 파일·오피스 문서)은 가볍게, 나머지는 보통 세기로 deflate 한다.
- ``stream`` 은 동기 생성기다. Starlette 의 StreamingResponse 는 동기 생성기를 워커 스레드에서 돌리므로
  파일 읽기·압축이 서빙 루프를 세우지 않는다.
"""

from __future__ import annotations

import datetime as _dt
import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, Iterable, Iterator, List, Mapping, Optional, Tuple

from fastapi import HTTPException

__all__ = [
    "CHUNK",
    "FAILED_NOTE",
    "MAX_BYTES",
    "MAX_FILES",
    "ZipFileEntry",
    "ZipPlan",
    "check_limits",
    "plan_tree",
    "safe_name",
    "stream",
    "unique_name",
]

logger = logging.getLogger("xgen_sdk.storage.folder_zip")

#: 한 번에 내려받는 파일 수 상한(기본).
MAX_FILES = 10_000
#: 한 번에 내려받는 합계 크기 상한(원본 크기 기준, 기본).
MAX_BYTES = 2 * 1024 * 1024 * 1024
#: 한 번에 읽어 zip 에 넣는 크기.
CHUNK = 1024 * 1024
#: 읽지 못한 파일을 적는 안내 파일 이름(zip 맨 위 폴더 안).
FAILED_NOTE = "받지 못한 파일.txt"

#: 이미 압축된 형식: 다시 압축해도 거의 줄지 않으므로 가장 빠른 세기로 넣는다.
_PACKED_EXTS = frozenset({
    "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst",
    "jpg", "jpeg", "png", "gif", "webp", "heic", "avif",
    "mp3", "mp4", "m4a", "m4v", "mov", "avi", "mkv", "webm", "ogg",
    "docx", "xlsx", "pptx", "hwpx", "odt", "ods", "odp", "pdf",
})
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")
#: zip 의 날짜는 1980 년부터다.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass
class ZipFileEntry:
    arcname: str
    item: Any


@dataclass
class ZipPlan:
    #: zip 맨 위 폴더 이름(= 내려받는 폴더 이름).
    root: str
    #: 만들 폴더(맨 위 포함, 부모가 먼저). 빈 폴더도 여기에 든다.
    dirs: List[str] = field(default_factory=list)
    files: List[ZipFileEntry] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def download_name(self) -> str:
        return f"{self.root}.zip"


def _fmt(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def safe_name(name: Any, fallback: str) -> str:
    """zip 안에서 쓸 이름 하나: 경로 구분자·제어 문자는 ``_``, 비었거나 ``.``·``..`` 이면 ``fallback``."""
    text = _BAD_CHARS.sub("_", str(name or "")).strip()
    return fallback if text in ("", ".", "..") else text


def unique_name(name: str, used: set, *, is_file: bool) -> str:
    """한 폴더 안에서 겹치지 않는 이름. 대소문자만 달라도 겹친다(풀어 놓는 운영체제가 그렇게 본다)."""
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    stem, dot, ext = name.rpartition(".") if is_file and "." in name.strip(".") else (name, "", "")
    for n in range(2, 100_000):
        candidate = f"{stem} ({n}){dot}{ext}"
        if candidate.casefold() not in used:
            used.add(candidate.casefold())
            return candidate
    raise HTTPException(status_code=409, detail=f"'{name}' 와 같은 이름이 너무 많습니다.")


def check_limits(count: int, total_bytes: int, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> None:
    """파일 수·합계 크기가 상한을 넘으면 413. 보내기 전에 부른다."""
    if count > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"파일이 {max_files:,}개를 넘어 한 번에 내려받을 수 없습니다. 하위 폴더별로 내려받으세요.",
        )
    if total_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(f"폴더가 너무 큽니다({_fmt(total_bytes)}). 한 번에 {_fmt(max_bytes)}까지 내려받을 수 있으니 "
                    "하위 폴더별로 내려받으세요."),
        )


def plan_tree(
    root_id: Hashable,
    root_name: Any,
    folders: Iterable[Mapping[str, Any]],
    files: Iterable[Mapping[str, Any]],
    *,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
) -> ZipPlan:
    """무엇을 어디에 넣을지 정한다. 상한을 넘으면 413(아무것도 보내지 않는다).

    ``folders`` 는 ``{"id", "parent_id", "name"}``(맨 위 폴더 아래의 폴더들, 맨 위 자신이 섞여 있어도 된다),
    ``files`` 는 ``{"folder_id", "name", "size", "item"}`` 이다. ``item`` 이 ``stream`` 의 ``download`` 에
    그대로 넘어간다. 맨 위 폴더에서 닿지 않는 폴더·파일은 넣지 않는다. 한 폴더 안의 순서는 폴더 이름순,
    파일은 받은 순서다.
    """
    file_list = list(files)
    total = sum(int(f.get("size") or 0) for f in file_list)
    check_limits(len(file_list), total, max_files=max_files, max_bytes=max_bytes)

    root = safe_name(root_name, "folder")
    out = ZipPlan(root=root, dirs=[root], total_bytes=total)
    children: Dict[Hashable, List[Mapping[str, Any]]] = {}
    for row in folders:
        if row.get("id") == root_id or row.get("parent_id") is None:
            continue
        children.setdefault(row["parent_id"], []).append(row)
    files_in: Dict[Hashable, List[Mapping[str, Any]]] = {}
    for f in file_list:
        files_in.setdefault(f.get("folder_id"), []).append(f)

    # 부모가 먼저 오게 넓이 우선으로 편다. 폴더와 파일은 한 폴더 안에서 이름 칸을 함께 쓴다.
    queue: List[Tuple[Hashable, str]] = [(root_id, root)]
    seen = {root_id}
    while queue:
        folder_id, path = queue.pop(0)
        used: set = set()
        for child in sorted(children.get(folder_id, []), key=lambda r: (str(r.get("name") or ""), str(r["id"]))):
            if child["id"] in seen:          # 데이터가 어긋나 고리가 생겨도 끝난다
                continue
            seen.add(child["id"])
            name = unique_name(safe_name(child.get("name"), "folder"), used, is_file=False)
            child_path = f"{path}/{name}"
            out.dirs.append(child_path)
            queue.append((child["id"], child_path))
        for f in files_in.get(folder_id, []):
            name = unique_name(safe_name(f.get("name"), "file"), used, is_file=True)
            out.files.append(ZipFileEntry(arcname=f"{path}/{name}", item=f.get("item", f)))
    return out


class _Sink(io.RawIOBase):
    """zipfile 이 쓰는 대로 모았다가 꺼내 가게 한다. 되감을 수 없으니 zipfile 은 크기를 뒤에 적는다."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: List[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def _default_date(item: Any) -> Optional[_dt.datetime]:
    if not isinstance(item, Mapping):
        return None
    value = item.get("updated_at") or item.get("created_at")
    if isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, _dt.datetime) else None


def _zip_date(value: Optional[_dt.datetime]) -> Tuple[int, int, int, int, int, int]:
    if not isinstance(value, _dt.datetime):
        value = _dt.datetime.now()
    if value.tzinfo is not None:
        value = value.astimezone()
    stamp = (value.year, value.month, value.day, value.hour, value.minute, value.second)
    return max(stamp, _ZIP_EPOCH)


def _level(name: str) -> int:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return 1 if ext in _PACKED_EXTS else 6


def stream(
    zplan: ZipPlan,
    download: Callable[[Any, str], None],
    *,
    chunk: int = CHUNK,
    date_of: Optional[Callable[[Any], Optional[_dt.datetime]]] = None,
    label: Callable[[Any], Any] = lambda item: item.get("id") if isinstance(item, Mapping) else item,
) -> Iterator[bytes]:
    """zip 바이트를 만든 만큼 내보낸다.

    ``download(item, path)`` 는 원본을 그 경로에 쓴다(복호화 포함). ``date_of(item)`` 은 zip 에 적을 수정 시각이다
    (없으면 ``updated_at``·``created_at``, 그것도 없으면 지금). ``label(item)`` 은 읽지 못했을 때 로그에 남길 이름.
    """
    sink = _Sink()
    failed: List[Tuple[str, str]] = []
    when = date_of or _default_date
    with tempfile.TemporaryDirectory(prefix="xgen-folder-zip-") as tmp:
        zf = zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        now = _zip_date(None)
        for path in zplan.dirs:
            info = zipfile.ZipInfo(path + "/", date_time=now)
            info.external_attr = (0o40755 << 16) | 0x10
            zf.writestr(info, b"")
        yield sink.take()

        blob = os.path.join(tmp, "blob")
        for entry in zplan.files:
            try:
                download(entry.item, blob)
            except Exception as exc:  # noqa: BLE001 - 한 파일 때문에 폴더 전체를 멈추지 않는다
                logger.warning("폴더 내려받기: 원본을 읽지 못했다 item=%s: %s", label(entry.item), exc)
                failed.append((entry.arcname, "원본을 읽지 못했습니다"))
                continue
            info = zipfile.ZipInfo(entry.arcname, date_time=_zip_date(when(entry.item)))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.compress_level = _level(entry.arcname)
            info.external_attr = 0o644 << 16
            info.file_size = os.path.getsize(blob)
            with open(blob, "rb") as src, zf.open(info, "w") as dst:
                while True:
                    data = src.read(chunk)
                    if not data:
                        break
                    dst.write(data)
                    out = sink.take()
                    if out:
                        yield out
            os.unlink(blob)
            out = sink.take()
            if out:
                yield out

        if failed:
            note = "다음 파일은 원본을 읽지 못해 이 zip 에 넣지 못했습니다.\n\n" + "".join(
                f"{path}: {why}\n" for path, why in failed)
            used = {f.arcname.casefold() for f in zplan.files} | {d.casefold() + "/" for d in zplan.dirs}
            name = f"{zplan.root}/{FAILED_NOTE}"
            if name.casefold() in used:
                name = f"{zplan.root}/_{FAILED_NOTE}"
            zf.writestr(zipfile.ZipInfo(name, date_time=now), note.encode("utf-8"))
        zf.close()
        yield sink.take()
    logger.info("폴더 내려받기 %s: 파일 %d개, %s, 폴더 %d개, 받지 못함 %d",
                zplan.download_name, len(zplan.files) - len(failed), _fmt(zplan.total_bytes),
                len(zplan.dirs), len(failed))
