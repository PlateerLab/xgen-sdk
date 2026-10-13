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
    def repl(self) -> Callable[["re.Match[str]"], str]:
        """``re.sub`` 에 넘길 치환자(매치마다 계산). 깨진 템플릿은 ``****`` 로 가린다(원문을 통과시키지 않는다)."""
        return self.replacement

    def _partial(self, matched: str) -> str:
        return partial_mask(matched, self.mask_char, self.keep_prefix, self.keep_suffix, self.preserve_format)

    def mask_match(self, matched: str) -> str:
        """매치 원문 하나 → 치환 결과(감사 샘플용). 앞뒤를 보는 정규식은 :meth:`replacement` 를 쓴다."""
        if self.masking_mode == MASK_MODE_PARTIAL:
            return self._partial(matched)
        try:
            return self.pattern.sub(self.mask, matched)
        except (re.error, IndexError):
            return DEFAULT_MASK

    def replacement(self, match: "re.Match[str]") -> str:
        """글 안에서 찾은 매치 → 그 자리의 치환 결과.

        매치 원문만 따로 떼어 다시 돌리면 앞뒤를 보는 정규식(``(?<=비밀번호: )\\S+`` · 앵커)이 맞지 않아 원문이
        그대로 나온다. 그래서 찾은 문맥의 매치 객체로 템플릿을 펼친다. 템플릿이 깨졌으면(없는 그룹 \\1)
        ``****`` 로 가린다(fail-closed).
        """
        if self.masking_mode == MASK_MODE_PARTIAL:
            return self._partial(match.group(0))
        try:
            return match.expand(self.mask)
        except (re.error, IndexError):
            return DEFAULT_MASK

    def substitute(self, text: str) -> str:
        """글 전체에 이 규칙을 적용."""
        return self.pattern.sub(self.replacement, text)


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
            samples.append({"original": original, "masked": rule.replacement(hit)})
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
    """가릴 구간 ``(원문 시작, 원문 끝, 치환문)``: 조각난 글(OOXML run · HTML 문자 참조 · PDF 줄)을 되돌려 쓸 때 쓴다.

    :func:`apply_rules` 와 **같은 결과**를 낸다: 규칙을 차례로, 앞 규칙이 가린 글 위에서 다시 찾는다(앞 마스크의
    일부까지 걸릴 수 있다). 글자마다 원문의 어느 구간에서 왔는지를 묶음으로 따라가 마지막에 원문 위치로 되짚는다.
    걸린 것이 없으면 아무것도 만들지 않는다(대부분의 문단).
    """
    if not text or not rules:
        return []
    current = text
    owner: Optional[List[int]] = None  # 지금 글의 글자마다 묶음 번호(처음 걸릴 때 만든다)
    parent: List[int] = []
    lo: List[int] = []
    hi: List[int] = []
    replaced: List[bool] = []

    def find(unit: int) -> int:
        while parent[unit] != unit:
            parent[unit] = parent[parent[unit]]
            unit = parent[unit]
        return unit

    def make(a: int, b: int, rep: bool) -> int:
        parent.append(len(parent))
        lo.append(a)
        hi.append(b)
        replaced.append(rep)
        return len(parent) - 1

    ghosts: List[int] = []  # 빈 치환문(글자가 남지 않는 마스크)의 묶음
    for rule in rules:
        matches = [m for m in rule.pattern.finditer(current) if m.end() > m.start()]
        if not matches:
            continue
        if owner is None:
            owner = [make(i, i + 1, False) for i in range(len(text))]
        out_text: List[str] = []
        out_owner: List[int] = []
        last = 0
        for match in matches:
            ms, me = match.span()
            out_text.append(current[last:ms])
            out_owner.extend(owner[last:ms])
            touched = {find(u) for u in owner[ms:me]}
            unit = make(min(lo[t] for t in touched), max(hi[t] for t in touched), True)
            for t in touched:
                parent[t] = unit
            rep = rule.replacement(match)
            if rep:
                out_text.append(rep)
                out_owner.extend([unit] * len(rep))
            else:
                ghosts.append(unit)
            last = me
        out_text.append(current[last:])
        out_owner.extend(owner[last:])
        current, owner = "".join(out_text), out_owner
    if owner is None:
        return []
    spans: List[Tuple[int, int, str]] = []
    seen: set = set()
    i, n = 0, len(current)
    while i < n:
        root = find(owner[i])
        j = i + 1
        while j < n and find(owner[j]) == root:
            j += 1
        if replaced[root]:
            spans.append((lo[root], hi[root], current[i:j]))
            seen.add(root)
        i = j
    for unit in ghosts:
        root = find(unit)
        if root not in seen:
            seen.add(root)
            spans.append((lo[root], hi[root], ""))
    spans.sort(key=lambda s: (s[0], s[1]))
    merged: List[Tuple[int, int, str]] = []
    for span in spans:
        # 빈 치환문 구간이 뒤 매치 구간 안에 들어간 경우(글자가 없어 묶이지 않았다): 합친다.
        if merged and span[0] < merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], span[1]), prev[2] + span[2])
        else:
            merged.append(span)
    return merged
