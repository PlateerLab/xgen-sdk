"""Term Expansion 인프라 — search_tools 의 query 확장 메커니즘.

엔진은 메커니즘만 가진다. 한국어/도메인 alias 같은 데이터는 외부 plug
(호스트 / 별도 패키지) 가 register/entry_points 로 주입한다.

엔진 본체에 도메인 단어 박지 않는 이유: 확장성·연동성·하드코딩X 원칙.
locale-ko / locale-ja 같은 언어별 alias 는 그 언어 사용자가 plug 로 합류.

확장 채널 2 가지:
  1. `register_term_expander(impl)` — TermExpander Protocol 만족하는 객체. 동적 알고리즘.
  2. `register_search_alias("네이버", ["naver"])` — 단순 1:N alias 등록 (lightweight).
  3. `entry_points("xgen_harness.term_expanders")` — 외부 패키지 자동 발견.

v1.0.x — 본 모듈은 기존 `xgen_harness/tools/builtin.py` 에서 분리됨 (god-class 정리).
하위 호환을 위해 builtin.py 에서 re-export 한다.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("harness.tools.term_expansion")


@runtime_checkable
class TermExpander(Protocol):
    """query term 확장 Protocol.

    구현체는 raw terms (사용자 query 의 토큰 리스트) 를 받아 확장된 terms 리스트
    반환. 한 term 을 다중 term 으로 펼치거나 (한국어→영문 alias), 변형 추가
    (어간 추출, transliteration 등) 가능. 빈 리스트 반환 = 확장 없음.

    구현은 stateless 권장 (Pipeline 마다 호출됨). 무거운 모델 로딩이 필요한
    경우 lazy init.
    """

    def expand(self, terms: list[str]) -> list[str]:
        """terms 를 확장. 입력 term 들도 결과에 포함시킬지는 구현 책임.

        통상 패턴:
            return list(terms) + [추가 alias / 변형 ...]
        """
        ...


# 외부에서 register 한 expander 들. dict 자체는 비어있고, plug 가 채운다.
_REGISTERED_EXPANDERS: list[TermExpander] = []
# raw term → 추가 term 들. register_search_alias 의 simple alias 등록용.
_SIMPLE_ALIASES: dict[str, list[str]] = {}
_ENTRY_POINTS_LOADED = False


def register_term_expander(expander: TermExpander) -> None:
    """search_tools 의 query 확장 expander 등록.

    예 (호스트 측 startup 에서):
        from xgen_harness.tools import register_term_expander
        class KoreanAliasExpander:
            def expand(self, terms): ...  # 한국어 단어 → 영문 alias
        register_term_expander(KoreanAliasExpander())
    """
    if not isinstance(expander, TermExpander):
        raise TypeError(f"expander must satisfy TermExpander Protocol: {type(expander).__name__}")
    _REGISTERED_EXPANDERS.append(expander)


def register_search_alias(term: str, aliases: list[str]) -> None:
    """단순 1:N alias 등록 (TermExpander 없이 빠른 사용).

    내부적으로 _SIMPLE_ALIASES dict 를 채우고, 단일 SimpleAliasExpander 가
    이를 사용. 호스트가 도메인 특화 매핑 한 줄로 추가하는 용도.

    예 (이식 측 — 한국어 alias):
        register_search_alias("네이버", ["naver"])
        register_search_alias("쇼핑", ["shopping", "shop"])
    """
    _SIMPLE_ALIASES[term] = list(aliases)


def list_term_expanders() -> list[TermExpander]:
    """등록된 expander 목록 (디버그/감사용)."""
    return list(_REGISTERED_EXPANDERS)


def list_search_aliases() -> dict[str, list[str]]:
    """등록된 simple alias 목록 (디버그/감사용)."""
    return dict(_SIMPLE_ALIASES)


def _load_entry_points_once() -> None:
    """``xgen_harness.term_expanders`` entry_points 자동 발견.

    1 회만 실행. 외부 패키지가 setup.py / pyproject.toml 에:
        [project.entry-points."xgen_harness.term_expanders"]
        ko_locale = "xgen_harness_locale_ko:KoreanAliasExpander"

    로 선언하면 첫 search_tools 호출 시 자동 등록.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        from importlib.metadata import entry_points
        try:
            eps = entry_points(group="xgen_harness.term_expanders")
        except TypeError:
            # Python < 3.10 fallback
            eps = entry_points().get("xgen_harness.term_expanders", [])
        for ep in eps:
            try:
                obj = ep.load()
                expander = obj() if isinstance(obj, type) else obj
                register_term_expander(expander)
                logger.info("[search_tools] loaded term_expander from entry_points: %s", ep.name)
            except Exception as e:
                logger.warning("[search_tools] entry_point %s load failed: %s", ep.name, e)
    except Exception as e:
        logger.debug("[search_tools] entry_points scan skipped: %s", e)


def _simple_alias_expand(terms: list[str]) -> list[str]:
    """_SIMPLE_ALIASES 기반 1:N 확장 — register_search_alias 의 데이터 사용."""
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        if t in seen:
            continue
        out.append(t)
        seen.add(t)
        for alias in _SIMPLE_ALIASES.get(t, []):
            if alias not in seen:
                out.append(alias)
                seen.add(alias)
    return out


def expand_query_terms(terms: list[str]) -> list[str]:
    """등록된 모든 expander + simple alias 를 거쳐 최종 확장된 term 리스트.

    엔진은 호출만 하고 결과 합집합. 데이터/도메인 지식 0 — 전부 외부 register
    또는 entry_points 에서 옴.
    """
    _load_entry_points_once()
    seen: set[str] = set()
    result: list[str] = []
    # 1) raw terms 보존
    for t in terms:
        if t not in seen:
            result.append(t)
            seen.add(t)
    # 2) simple alias 확장
    for t in _simple_alias_expand(terms):
        if t not in seen:
            result.append(t)
            seen.add(t)
    # 3) 외부 등록 expander 들
    for exp in _REGISTERED_EXPANDERS:
        try:
            extra = exp.expand(list(terms))
        except Exception as e:
            logger.warning("[search_tools] expander %s failed: %s", type(exp).__name__, e)
            continue
        for t in extra or []:
            if t not in seen:
                result.append(t)
                seen.add(t)
    return result


# v1.0.x — 구 builtin.py 의 private alias `_expand_query_terms` 호환 (외부 import 가능성).
_expand_query_terms = expand_query_terms
