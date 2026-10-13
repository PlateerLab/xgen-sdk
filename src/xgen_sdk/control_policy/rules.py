"""통제 정책 규칙: 정책 행 하나를 정규식과 치환 방식으로 만들고 글에 적용한다.

정책 행(``piis`` · ``forbidden_words``)의 뜻:

    regex_pattern    찾을 정규식
    masking          가릴지. 끄면 화면의 [마스킹 미적용]
    masking_mode     full: masking_pattern 으로 매치 전체를 바꾼다(역참조 \\1 지원)
                     partial: 앞 keep_prefix · 뒤 keep_suffix 글자를 남기고 나머지를 mask_char 로
    preserve_format  partial 에서 구분자(영숫자가 아닌 글자: -, @, . 등)는 남긴다

[마스킹 미적용] 정책을 어떻게 다룰지는 쓰는 자리가 정한다(:func:`compile_rules` 의 ``unmasked``).

    "skip"      관리자가 "가리지 말라" 고 한 것이라 규칙에서 뺀다: 에이전트 적용 · 채팅 차단 · 업로드 제거
    "default"   ``****`` 로 가린다(fail-closed): 문서 업로드에서 사람이 고른 정책 · 출처 원본 서빙
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("xgen-sdk.control-policy")

#: masking 이 꺼졌거나 패턴이 비어도 원문은 내보내지 않는다.
DEFAULT_MASK = "****"
MASK_MODE_FULL = "full"
MASK_MODE_PARTIAL = "partial"
#: 감사 기록에 남기는 정책별 매치 샘플 수.
SAMPLE_LIMIT = 3


def partial_mask(
    matched: str,
    mask_char: str = "*",
    keep_prefix: int = 0,
    keep_suffix: int = 0,
    preserve_format: bool = True,
) -> str:
    """부분 마스킹: 앞 ``keep_prefix`` · 뒤 ``keep_suffix`` 글자는 원문, 나머지는 ``mask_char`` 로 1:1.

    유지 글자 수 합이 매치 길이 이상이면 전부 가린다(fail-closed: 짧은 매치가 보존 구간이 겹쳐
    원문 그대로 나가는 것을 막는다). ``preserve_format`` 이면 구분자는 어느 구간에서든 원문으로 둔다.
    예: ``010-1234-5678``, 뒤 4 유지 → ``***-****-5678``.
    """
    if not matched:
        return matched
    ch = (mask_char or "*")[0]
    keep_p = max(0, int(keep_prefix or 0))
    keep_s = max(0, int(keep_suffix or 0))
    n = len(matched)
    lo, hi = (0, n) if keep_p + keep_s >= n else (keep_p, n - keep_s)
    return "".join(
        c if (i < lo or i >= hi or (preserve_format and not c.isalnum())) else ch
        for i, c in enumerate(matched)
    )


@dataclass(frozen=True)
class MaskingRule:
    """컴파일된 정책 하나."""

    policy_id: Optional[int]
    policy_name: str
    policy_type: str
    pattern: "re.Pattern[str]"
    #: full 모드의 치환 문자열(템플릿, 역참조 확장).
    mask: str = DEFAULT_MASK
    masking_mode: str = MASK_MODE_FULL
    mask_char: str = "*"
    keep_prefix: int = 0
    keep_suffix: int = 0
    preserve_format: bool = True

    @property
    def repl(self) -> Union[str, Callable[["re.Match[str]"], str]]:
        """``re.sub`` 에 넘길 치환자: full 은 템플릿 문자열, partial 은 매치마다 계산하는 함수."""
        if self.masking_mode == MASK_MODE_PARTIAL:
            return lambda m: self._partial(m.group(0))
        return self.mask

    def _partial(self, matched: str) -> str:
        return partial_mask(matched, self.mask_char, self.keep_prefix, self.keep_suffix, self.preserve_format)

    def mask_match(self, matched: str) -> str:
        """매치 원문 하나 → 치환 결과(샘플 · 조각 텍스트 되쓰기용)."""
        if self.masking_mode == MASK_MODE_PARTIAL:
            return self._partial(matched)
        return self.pattern.sub(self.mask, matched)

    def substitute(self, text: str) -> str:
        """글 전체에 이 규칙을 적용."""
        return self.pattern.sub(self.repl, text)


def compile_rules(rows: Optional[Iterable[Dict[str, Any]]], policy_type: str, *,
                  unmasked: str = "skip") -> List[MaskingRule]:
    """정책 행 → 규칙. 비거나 깨진 정규식은 건너뛴다(한 정책이 나머지를 막지 않는다)."""
    if unmasked not in ("skip", "default"):
        raise ValueError(f"unmasked 는 skip 또는 default: {unmasked}")
    rules: List[MaskingRule] = []
    for row in rows or []:
        masking = bool(row.get("masking"))
        if not masking and unmasked == "skip":
            continue
        regex = row.get("regex_pattern")
        if not regex:
            continue
        try:
            pattern = re.compile(regex)
        except re.error as exc:
            logger.warning("%s 정규식 오류 '%s': %s", policy_type, row.get("policy_name"), exc)
            continue
        mode = str(row.get("masking_mode") or MASK_MODE_FULL).strip().lower()
        rules.append(MaskingRule(
            policy_id=row.get("id"),
            policy_name=str(row.get("policy_name") or ""),
            policy_type=policy_type,
            pattern=pattern,
            mask=(row.get("masking_pattern") if masking else None) or DEFAULT_MASK,
            # masking 이 꺼진 행을 가리게 될 때(default)는 부분 마스킹도 전체 치환으로 내린다.
            masking_mode=MASK_MODE_PARTIAL if (mode == MASK_MODE_PARTIAL and masking) else MASK_MODE_FULL,
            mask_char=str(row.get("mask_char") or "*"),
            keep_prefix=int(row.get("keep_prefix") or 0),
            keep_suffix=int(row.get("keep_suffix") or 0),
            preserve_format=bool(row.get("preserve_format", True)),
        ))
    return rules


@dataclass
class MaskingResult:
    """적용 결과 + 감사 기록용 집계."""

    text: str
    #: 정책별 ``{"policy_id", "policy_name", "policy_type", "count", "samples"}``.
    detections: List[Dict[str, Any]] = field(default_factory=list)
    total_detected: int = 0
    changed_policy_ids: List[int] = field(default_factory=list)
    changed_policy_names: List[str] = field(default_factory=list)
    policies_used: int = 0

    @property
    def changed_policy_count(self) -> int:
        return len(self.changed_policy_ids)

    def by_type(self) -> Dict[str, List[Dict[str, Any]]]:
        """정책 종류별 탐지(통제 정책 로그는 종류마다 한 줄이다)."""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for entry in self.detections:
            out.setdefault(entry.get("policy_type") or "", []).append(entry)
        return out


def apply_rules(text: str, rules: Sequence[MaskingRule], *, mask: bool = True) -> MaskingResult:
    """규칙을 차례대로 적용한다. ``mask=False`` 면 찾기만 한다.

    앞 규칙이 가린 자리는 뒤 규칙이 다시 보지 않는다(가린 글 위에서 찾는다).
    샘플은 ``finditer`` 의 ``group(0)``: ``findall`` 은 그룹 정규식에서 튜플을 돌려줘 깨진다.
    """
    result = MaskingResult(text=text, policies_used=len(rules))
    if not text or not rules:
        return result
    for rule in rules:
        try:
            hits = list(rule.pattern.finditer(result.text))
        except re.error as exc:  # pragma: no cover, compile 을 통과한 패턴은 여기서 안 깨진다
            logger.warning("%s 정규식 오류 '%s': %s", rule.policy_type, rule.policy_name, exc)
            continue
        if not hits:
            continue
        samples: List[Dict[str, str]] = []
        seen: set = set()
        for hit in hits:
            original = hit.group(0)
            if original in seen:
                continue
            seen.add(original)
            samples.append({"original": original, "masked": rule.mask_match(original)})
            if len(samples) >= SAMPLE_LIMIT:
                break
        result.detections.append({
            "policy_id": rule.policy_id,
            "policy_name": rule.policy_name,
            "policy_type": rule.policy_type,
            "count": len(hits),
            "samples": samples,
        })
        result.total_detected += len(hits)
        if not mask:
            continue
        masked = rule.substitute(result.text)
        if masked != result.text:
            result.text = masked
            if rule.policy_id is not None:
                result.changed_policy_ids.append(rule.policy_id)
            result.changed_policy_names.append(rule.policy_name)
    return result


def find_masked_spans(text: str, rules: Sequence[MaskingRule]) -> List[Tuple[int, int, str]]:
    """가릴 구간 ``(시작, 끝, 치환문)``: 조각난 글(OOXML run)을 되돌려 쓸 때 쓴다.

    겹치는 매치는 앞 규칙이 이긴다(:func:`apply_rules` 의 차례를 재현).
    """
    spans: List[Tuple[int, int, str]] = []
    if not text or not rules:
        return spans
    claimed: List[Tuple[int, int]] = []
    for rule in rules:
        for match in rule.pattern.finditer(text):
            start, end = match.span()
            if start == end:
                continue
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            spans.append((start, end, rule.mask_match(match.group(0))))
    spans.sort(key=lambda s: s[0])
    return spans
