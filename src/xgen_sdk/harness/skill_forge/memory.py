"""SF7 — 증거로 등급을 매기는 기억(evidence-graded memory).

**왜 다르게 만드는가.** Geny 는 2026-07-23 에 메모리 거버넌스를 붙였다(항목별 trust,
모순 감지). 규율이 좋다 — 특히 "보였는데 안 썼다"를 음의 신호로 자동 배선하지 않은
판단은 옳다. 그건 나쁜 질의를 애먼 노트 탓으로 돌리는 신호이기 때문이다.

그런데 그 판단은 **신호가 대리(proxy)라서** 나온 제약이다. 편집·사용 여부는 유용함의
그림자일 뿐이다. 우리에겐 스킬을 검증하는 재현벤치가 이미 있고, **기억 항목의 값어치는
스킬과 똑같은 방법으로 직접 잴 수 있다** — 그 항목을 빼고 재생해서 점수가 떨어지면
그 항목이 일한 것이다. 대리 신호가 아니라 측정이다. 그래서 여기서는:

- **trust 는 측정으로만 움직인다.** 조회·노출·편집 같은 대리 신호로는 절대 안 움직인다.
- **모순은 드러내는 데서 끝내지 않고 증거로 판정한다.** 충돌하는 두 항목을 각각 넣고
  재생해 이긴 쪽을 남긴다. 변별이 안 되면 둘 다 `disputed` 로 두고 사람에게 넘긴다.
- **자동 삭제는 없다.** 은퇴는 측정된 해악이나 사람의 결정으로만. 되돌릴 수 있어야 한다.
- **감쇠는 중립으로.** 오래 재측정되지 않은 확신은 중립으로 흘러 경화(ossification)를 막는다.
  0 으로 떨어뜨리지 않는 이유: 안 재봤다는 것은 나쁘다는 뜻이 아니다.

계약은 게이트와 같은 것을 쓴다(`BenchCase` + score 함수) — 기억과 스킬이 같은 잣대를
쓰는 것이 이 설계의 요점이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .gate import BenchCase

NEUTRAL = 0.5
MEMORY_STATUSES = ("active", "disputed", "retired")

# (기억 텍스트, 케이스) -> 점수. 기억을 넣고 재생했을 때의 점수를 준다.
# skill 게이트의 ScoreFn 과 같은 모양이라 호스트가 하나의 러너를 둘 다에 쓸 수 있다.
MemoryScoreFn = Callable[[BenchCase, "MemoryItem | None"], float]


@dataclass
class MemoryItem:
    """기억 한 항목. 값어치는 주장이 아니라 측정으로 붙는다."""

    item_id: str
    text: str
    scope: str = "user"
    trust: float = NEUTRAL
    status: str = "active"
    source_run_ids: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in MEMORY_STATUSES:
            raise ValueError(f"unknown memory status: {self.status}")
        self.trust = min(1.0, max(0.0, float(self.trust)))

    @property
    def measured(self) -> bool:
        """한 번이라도 재현벤치로 재본 적이 있는가. 없으면 trust 는 그냥 중립이다."""
        return bool(self.evidence.get("measurements"))


@dataclass
class Measurement:
    item_id: str
    cases: int
    with_item: float
    without_item: float

    @property
    def gain(self) -> float:
        return self.with_item - self.without_item

    def verdict(self, min_gain: float, harm_threshold: float) -> str:
        if self.gain > min_gain:
            return "helpful"
        if self.gain < -abs(harm_threshold):
            return "harmful"
        return "neutral"


@dataclass
class MemoryPolicy:
    min_cases: int = 4
    min_gain: float = 0.05
    """이 이상 올려야 도움이 됐다고 본다.

    0 이면 안 된다 — 잡음 수준의 +0.001 도 helpful 로 세어 **trust 가 한쪽으로만
    흐른다**(검수에서 실제로 잡힌 편향: +0.001 은 trust 를 올리는데 -0.001 은
    중립이었다). 해악 문턱과 같은 값으로 둬서 잡음 구간을 대칭으로 만든다.
    """
    harm_threshold: float = 0.05
    """이 이상 점수를 떨어뜨리면 해롭다고 본다 — 잡음과 구분하려고 0 이 아니다."""
    step: float = 0.15
    """한 번의 측정이 trust 를 옮기는 폭. 한 방에 확신하지 않는다."""
    decay: float = 0.05
    """재측정 없이 흐르면 중립으로 끌어당기는 폭(경화 방지)."""


def measure(item: MemoryItem, score: MemoryScoreFn, cases: Sequence[BenchCase],
            policy: MemoryPolicy | None = None) -> Measurement | None:
    """항목을 넣고/빼고 재생해 실제 기여를 잰다. 케이스가 모자라면 재지 않는다.

    측정할 수 없을 때 추측으로 채우지 않는 것이 요점이다 — 스킬 게이트가
    케이스 부족을 `staged` 로 정직하게 처리하는 것과 같은 규율이다.
    """
    policy = policy or MemoryPolicy()
    if len(cases) < policy.min_cases:
        return None
    with_item = sum(score(case, item) for case in cases) / len(cases)
    without_item = sum(score(case, None) for case in cases) / len(cases)
    return Measurement(item_id=item.item_id, cases=len(cases),
                       with_item=with_item, without_item=without_item)


def apply_measurement(item: MemoryItem, measurement: Measurement,
                      policy: MemoryPolicy | None = None) -> MemoryItem:
    """측정 결과만이 trust 를 움직인다."""
    policy = policy or MemoryPolicy()
    verdict = measurement.verdict(policy.min_gain, policy.harm_threshold)
    before = item.trust
    if verdict == "helpful":
        item.trust = min(1.0, item.trust + policy.step)
    elif verdict == "harmful":
        item.trust = max(0.0, item.trust - policy.step)
    # neutral 은 움직이지 않는다 — "차이가 없다"는 "나쁘다"가 아니다.
    item.evidence.setdefault("measurements", []).append({
        "cases": measurement.cases,
        "gain": round(measurement.gain, 4),
        "verdict": verdict,
    })
    item.history.append(
        f"measure: {verdict} gain={measurement.gain:+.3f} "
        f"trust {before:.2f}->{item.trust:.2f} (n={measurement.cases})")
    return item


def decay_unmeasured(items: Iterable[MemoryItem],
                     policy: MemoryPolicy | None = None) -> list[MemoryItem]:
    """재측정되지 않은 확신을 중립으로 끌어당긴다.

    0 으로 보내지 않는다 — 안 재봤다는 것이 나쁘다는 뜻은 아니기 때문이다.
    측정 이력이 있어도 계속 감쇠시키는 이유: 세상이 변하면 옛 측정은 낡는다.
    """
    policy = policy or MemoryPolicy()
    moved = []
    for item in items:
        if item.status == "retired":
            continue
        if abs(item.trust - NEUTRAL) < 1e-9:
            continue
        direction = -1.0 if item.trust > NEUTRAL else 1.0
        stepped = item.trust + direction * policy.decay
        item.trust = NEUTRAL if (item.trust - NEUTRAL) * (stepped - NEUTRAL) <= 0 else stepped
        moved.append(item)
    return moved


# --------------------------------------------------------------- 모순

# 한국어는 조사가 붙어 단어 경계(`\b`)가 생기지 않는다 — "하지 않는다" 에서 `\b않\b`
# 는 절대 안 맞는다(실제로 안 맞아서 감지가 0건이었다). 그래서 언어별로 나눈다.
_NEGATION_EN = re.compile(r"\b(not|never|no longer|isn't|aren't|doesn't|don't|won't)\b",
                          re.IGNORECASE)
_NEGATION_KO = re.compile(r"(않|없|아니|못)")

_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)")
_STOP = {"the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
         "and", "or", "for", "with", "that", "this", "it", "be", "as", "at",
         "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도"}


def _tokens(text: str) -> set[str]:
    """형태소 분석기 없이 자르므로 한국어는 조사가 붙은 채로 남는다("배포는" ≠
    "배포가"). 그래서 모순 감지는 **재현율보다 정밀도**를 택한 장치다 — 놓치는
    모순은 있어도, 엉뚱한 쌍을 모순이라 우기지는 않는다."""
    words = re.findall(r"[a-z0-9]+|[가-힣]+", (text or "").lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def _has_negation(text: str) -> bool:
    return bool(_NEGATION_EN.search(text or "") or _NEGATION_KO.search(text or ""))


def _strip_negation(text: str) -> str:
    return _NEGATION_KO.sub(" ", _NEGATION_EN.sub(" ", text or ""))


@dataclass
class Contradiction:
    left: str
    right: str
    kind: str          # negation_flip | numeric_conflict
    overlap: float
    detail: str = ""


def detect_contradictions(items: Sequence[MemoryItem],
                          min_overlap: float = 0.5,
                          min_tokens: int = 1) -> list[Contradiction]:
    """같은 것을 말하는데 반대로 말하는 쌍을 찾는다.

    두 가지만 본다 — 부정 뒤집힘과 같은 자리의 숫자 충돌. 의미 모순 전반을
    LLM 으로 판정하지 않는 이유: 판정에 LLM 을 쓰면 이 계층이 모델 품질에
    끌려간다(우리 벤치가 LLM-judge 를 안 쓰는 것과 같은 이유).
    """
    found: list[Contradiction] = []
    active = [i for i in items if i.status != "retired"]
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            if left.scope != right.scope:
                continue
            left_core, right_core = _tokens(_strip_negation(left.text)), _tokens(
                _strip_negation(right.text))
            # `min_tokens` 로 얇은 근거를 걸러낼 수 있다(기본 1 = 안 거름).
            # 2 로 올리면 "threshold is 0.8" 처럼 핵심 토큰이 하나인 **정당한** 모순도
            # 같이 죽는다 — 검수에서 확인했다. 그래서 기본값은 민감하게 두고,
            # 오탐의 대가는 `resolve` 가 감당한다(둘 다 disputed → 사람이 본다).
            if min(len(left_core), len(right_core)) < min_tokens:
                continue
            overlap = len(left_core & right_core) / min(len(left_core), len(right_core))
            if overlap < min_overlap:
                continue
            left_neg = _has_negation(left.text)
            right_neg = _has_negation(right.text)
            if left_neg != right_neg:
                found.append(Contradiction(left.item_id, right.item_id,
                                           "negation_flip", round(overlap, 3),
                                           "한쪽만 부정형"))
                continue
            # 문자열로 비교하면 "0.8" 과 "0.80" 이 충돌로 잡힌다(검수에서 확인).
            # 같은 값의 다른 표기는 모순이 아니다.
            left_nums = [float(n) for n in _NUMBER.findall(left.text)]
            right_nums = [float(n) for n in _NUMBER.findall(right.text)]
            if left_nums and right_nums and left_nums != right_nums:
                found.append(Contradiction(left.item_id, right.item_id,
                                           "numeric_conflict", round(overlap, 3),
                                           f"{left_nums} vs {right_nums}"))
    return found


@dataclass
class Resolution:
    contradiction: Contradiction
    winner: str = ""
    loser: str = ""
    decided: bool = False
    reason: str = ""


def resolve(contradiction: Contradiction, left: MemoryItem, right: MemoryItem,
            score: MemoryScoreFn, cases: Sequence[BenchCase],
            policy: MemoryPolicy | None = None) -> Resolution:
    """모순을 증거로 판정한다 — 드러내고 끝내지 않는다.

    둘을 각각 넣고 재생해 더 나은 쪽을 남긴다. 차이가 잡음 수준이면 판정하지
    않고 **둘 다 `disputed`** 로 둔다. 자동 삭제는 없다 — 진 쪽도 은퇴가 아니라
    `disputed` 다. 우리가 틀렸을 때 되돌릴 수 있어야 하기 때문이다.
    """
    policy = policy or MemoryPolicy()
    left_measurement = measure(left, score, cases, policy)
    right_measurement = measure(right, score, cases, policy)
    if left_measurement is None or right_measurement is None:
        left.status = right.status = "disputed"
        return Resolution(contradiction, reason="케이스 부족 — 판정 불가, 사람 확인 필요")

    apply_measurement(left, left_measurement, policy)
    apply_measurement(right, right_measurement, policy)
    margin = left_measurement.gain - right_measurement.gain
    if abs(margin) <= policy.harm_threshold:
        # 둘 다 보류한다 — 서로 반대되는 두 사실을 동시에 믿는 것이 더 나쁘다.
        # 대신 **왜 보류됐는지**를 양쪽 이력에 남긴다. 오탐이면 사람이 여기서 되돌린다.
        def note(other: MemoryItem) -> str:
            return (f"disputed: {contradiction.kind}({contradiction.detail}) "
                    f"상대={other.item_id} — 재현벤치가 변별 못 함(차이 {margin:+.3f})")

        left.status = right.status = "disputed"
        left.history.append(note(right))
        right.history.append(note(left))
        return Resolution(contradiction,
                          reason=f"변별 없음(차이 {margin:+.3f}) — 사람 확인 필요")

    winner, loser = (left, right) if margin > 0 else (right, left)
    winner.status = "active"
    loser.status = "disputed"      # 삭제하지 않는다
    loser.history.append(f"disputed: {winner.item_id} 이 재현벤치에서 {abs(margin):.3f} 앞섬")
    return Resolution(contradiction, winner=winner.item_id, loser=loser.item_id,
                      decided=True,
                      reason=f"재현벤치 판정: {winner.item_id} 우세({abs(margin):+.3f})")


# --------------------------------------------------------------- 회상

def recall(items: Sequence[MemoryItem], limit: int = 5,
           min_trust: float = NEUTRAL) -> list[MemoryItem]:
    """프롬프트에 넣을 항목 고르기.

    `disputed` 는 절대 넣지 않는다 — 모순이 해결되기 전에 주입하면 에이전트가
    서로 반대되는 두 사실을 동시에 믿게 된다. 미측정 항목은 중립이라 기본
    문턱(0.5)에 걸려 들어오지만, 측정으로 해롭다고 나온 것은 걸러진다.
    """
    usable = [i for i in items if i.status == "active" and i.trust >= min_trust]
    usable.sort(key=lambda i: (-i.trust, i.item_id))
    return usable[:limit]
