"""관측 중복 제거 — 검색/도구 결과가 같은 본문을 반복 누적하는 것을 막는 일반 유틸.

엔진 어디에도 도메인 어휘 없음. RecallSet([[recall]]) 의 keep dedup 키로 쓰이고,
이식측은 s07 관측 적재 경로에서 `dedupe(...)` 로 누적 노이즈를 직접 줄일 수 있다.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Iterable, Optional

_WS_RE = re.compile(r"\s+")


def content_fingerprint(text: str, *, normalize: bool = True) -> str:
    """내용 지문 — dedup 안정 키. normalize 면 strip+소문자+공백압축 후 sha1.

    정규화는 "공백/대소문자만 다른 같은 본문" 을 같은 지문으로 묶기 위함.
    바이트가 아니라 의미가 같으면 같은 키가 되도록.
    """
    s = text or ""
    if normalize:
        s = _WS_RE.sub(" ", s.strip().lower())
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def dedupe(
    items: Iterable[Any],
    *,
    key: Optional[Callable[[Any], Any]] = None,
) -> list[Any]:
    """순서 보존 중복 제거. key(item) 이 같은 첫 항목만 남긴다(기본 key = item 자체).

    문자열 본문 dedup 은 `key=content_fingerprint` 로 호출.
    """
    seen: set[Any] = set()
    out: list[Any] = []
    for it in items:
        k = key(it) if key is not None else it
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out
