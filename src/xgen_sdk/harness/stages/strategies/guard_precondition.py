"""
ToolPreconditionGuard — 범용 도구 호출 선행조건 Guard (v0.17.0)

롯데/다른 고객사의 "도구 A 를 호출하기 전에 도구 B 가 호출됐는지, 그리고
A 의 페이로드가 특정 조건일 때만 요구하는지" 같은 규칙을 **누구나 params 로
선언** 할 수 있게 한다. 엔진에는 클라이언트명/도구명 하드코딩 전무.

## 사용 예
```
stage_params["s05_policy"] = {
    "guards": [
        {
            "name": "tool_precondition",
            "params": {
                "rules": [
                    {
                        # 대상 도구 이름 (정확히 일치)
                        "tool": "submit_result",
                        # 선행 도구 호출 요구. 여러 개면 AND.
                        "require_prior": [
                            {"tool": "iterative_document_search", "min_count": 1},
                        ],
                        # (선택) 페이로드 조건 — 이 조건이 참일 때만 규칙 적용.
                        # 미지정이면 항상 적용. 매칭은 JSONPath-like 경로.
                        "when": {
                            "path": "fileNo[*].status",
                            "equals": "01",
                        },
                        # LLM 에게 돌려줄 메시지 (가짜 tool_result).
                        "message": "시험성적서 합격 판정 전에 QA 기준을 iterative_document_search 로 조회하고 측정값과 기준값을 비교하세요."
                    },
                ]
            }
        }
    ]
}
```

## 규칙 항목
- `tool` (필수): 검사 대상 도구 이름.
- `require_prior` (필수): `[{tool, min_count}]` 리스트. 모두 만족해야 통과.
- `when` (선택): 페이로드가 이 조건을 만족할 때만 규칙 적용.
    - `path`: 간이 JSONPath — 예: `"fileNo[*].status"` = fileNo 리스트 각 원소의 status
    - `equals`: 값 비교 (단일 값 또는 list). path 가 배열 형태면 "any-match" 로 평가.
- `message` (선택): 차단 시 LLM 에게 전달할 설명. 없으면 자동 생성.

## 왜 여기 있는가
훅 포인트 PRE_TOOL 에서 동작. Policy Gate 가 각 pending_tool_call 별로 Guard
체인을 돌릴 때, 이 Guard 는 rules 목록 중 해당 tool 과 일치하는 규칙을 찾아
선행 호출 이력 (state.tool_call_history) 과 페이로드를 대조.

## 하드코딩 제로 원칙
- 클라이언트명 / 도구명 코드 내부 전무 — 전부 `params["rules"]` 로 주입.
- `xgen_harness.guards` entry_points 에 등록되어 UI 드롭다운 자동 노출.
- 규칙은 Gallery 패키지 데이터 (YAML/JSON 파일 로드 후 params 로 전달) 로 관리 가능.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .guard import FieldSchema, Guard, GuardResult, HookContext, HookPoint

logger = logging.getLogger("harness.strategy.guard_precondition")


class ToolPreconditionGuard(Guard):
    """도구 호출 전 선행 호출 이력/페이로드 조건을 검사하는 범용 Guard.

    규칙은 params.rules 배열로 선언. 각 규칙 = {tool, require_prior, when?, message?}.
    코드에 클라이언트명/도구명 하드코딩 전무 — 전부 데이터 주입.
    """

    def __init__(self, rules: Optional[list[dict[str, Any]]] = None):
        self._rules: list[dict[str, Any]] = list(rules or [])

    @property
    def name(self) -> str:
        return "tool_precondition"

    @property
    def hook_points(self) -> set[HookPoint]:
        return {HookPoint.PRE_TOOL}

    @classmethod
    def param_schema(cls) -> list[FieldSchema]:
        return [
            FieldSchema(
                id="rules",
                type="rule_list",
                default=[],
                item_schema=[
                    FieldSchema(id="tool", type="text", default="", required=True),
                    FieldSchema(id="require_prior", type="textarea", default="[]"),
                    FieldSchema(id="when", type="textarea", default=""),
                    FieldSchema(id="message", type="textarea", default=""),
                ],
            ),
        ]

    def configure(self, config: dict[str, Any]) -> None:
        raw = config.get("rules")
        if raw is None:
            return
        self._rules = self._normalize_rules(raw)

    def check(self, state: Any, context: HookContext) -> GuardResult:
        tc = context.pending_tool_call or {}
        tool_name = tc.get("tool_name", "")
        if not tool_name:
            return GuardResult(passed=True, guard_name=self.name)

        applicable = [r for r in self._rules if r.get("tool") == tool_name]
        if not applicable:
            return GuardResult(passed=True, guard_name=self.name)

        history: list[dict[str, Any]] = getattr(state, "tool_call_history", []) or []
        tool_input = tc.get("tool_input") or {}

        for rule in applicable:
            # when 조건 평가 (미지정이면 항상 적용)
            when = rule.get("when")
            if when and not self._match_when(tool_input, when):
                continue

            # require_prior 각 요구사항 검증
            missing: list[str] = []
            for req in rule.get("require_prior") or []:
                req_tool = req.get("tool")
                min_count = int(req.get("min_count", 1) or 1)
                if not req_tool:
                    continue
                observed = sum(1 for h in history if h.get("tool_name") == req_tool)
                if observed < min_count:
                    missing.append(f"{req_tool}≥{min_count} (실측 {observed})")

            if missing:
                message = rule.get("message") or (
                    f"'{tool_name}' 호출 전 선행 조건 미충족: {', '.join(missing)}"
                )
                return GuardResult(
                    passed=False,
                    guard_name=self.name,
                    reason=f"선행조건 미충족 ({tool_name}): {', '.join(missing)}",
                    severity="block",
                    tool_error_message=message,
                )

        return GuardResult(passed=True, guard_name=self.name)

    # ── 내부 헬퍼 ──────────────────────────────────────

    @staticmethod
    def _normalize_rules(raw: Any) -> list[dict[str, Any]]:
        """rules 입력을 list[dict] 로 정규화.

        UI 의 textarea 는 JSON 문자열로 올 수 있음 — require_prior / when 이 문자열이면
        json.loads 시도. 실패 시 해당 필드만 무시하고 나머지는 유지.
        """
        import json as _json

        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                return []

        if not isinstance(raw, list):
            return []

        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            rule = dict(item)

            # require_prior 문자열 JSON 파싱
            if isinstance(rule.get("require_prior"), str):
                try:
                    rule["require_prior"] = _json.loads(rule["require_prior"])
                except Exception:
                    rule["require_prior"] = []

            # v0.29.4 — list[str] 단축형 정규화. 사용자가 ["list_files"] 처럼 도구 이름만
            # 적어도 [{"tool": "list_files"}] 로 자동 변환. list[dict] 강제는 사용자 친화도 낮음.
            rp = rule.get("require_prior")
            if isinstance(rp, list):
                rule["require_prior"] = [
                    {"tool": item} if isinstance(item, str) else item
                    for item in rp
                ]

            # when 문자열 JSON 파싱 (빈 문자열은 조건 없음)
            if isinstance(rule.get("when"), str):
                when_s = rule["when"].strip()
                if not when_s:
                    rule.pop("when", None)
                else:
                    try:
                        rule["when"] = _json.loads(when_s)
                    except Exception:
                        rule.pop("when", None)

            out.append(rule)
        return out

    def _match_when(self, tool_input: dict[str, Any], when: dict[str, Any]) -> bool:
        """페이로드에 when 조건이 맞는지. 간이 JSONPath 평가.

        지원 path 패턴:
          - "key"                  — 단일 값
          - "key.sub"               — 중첩 키
          - "key[*].sub"            — 배열 원소 순회 + sub 키 추출 (any-match)
          - "key[0].sub"            — 인덱스 직접 지정
        equals 값은 단일 또는 list. 배열 결과면 한 원소라도 equals 에 매칭되면 True.
        """
        path = when.get("path") if isinstance(when, dict) else None
        expected = when.get("equals") if isinstance(when, dict) else None
        if not path:
            return True  # path 없으면 무조건 적용

        values = self._resolve_path(tool_input, path)
        if not values:
            return False

        expected_list: list[Any]
        if isinstance(expected, list):
            expected_list = expected
        else:
            expected_list = [expected]

        # str/int 비교 용 정규화
        def _norm(v: Any) -> Any:
            return v if not isinstance(v, str) else v.strip()

        for v in values:
            for e in expected_list:
                if _norm(v) == _norm(e):
                    return True
        return False

    @staticmethod
    def _resolve_path(data: Any, path: str) -> list[Any]:
        """간이 JSONPath 평가. 매칭된 값 리스트 반환 (path 가 배열 경유면 여러 값)."""
        tokens: list[tuple[str, Any]] = []
        i = 0
        buf = ""
        while i < len(path):
            c = path[i]
            if c == ".":
                if buf:
                    tokens.append(("key", buf))
                    buf = ""
                i += 1
                continue
            if c == "[":
                if buf:
                    tokens.append(("key", buf))
                    buf = ""
                # 닫는 ] 까지 읽기
                end = path.find("]", i)
                if end == -1:
                    return []
                inner = path[i + 1:end]
                if inner == "*":
                    tokens.append(("star", None))
                else:
                    try:
                        tokens.append(("idx", int(inner)))
                    except ValueError:
                        return []
                i = end + 1
                continue
            buf += c
            i += 1
        if buf:
            tokens.append(("key", buf))

        # 토큰 순차 평가 — current 는 list[Any] 로 유지
        current: list[Any] = [data]
        for kind, arg in tokens:
            nxt: list[Any] = []
            for v in current:
                if kind == "key":
                    if isinstance(v, dict) and arg in v:
                        nxt.append(v[arg])
                elif kind == "idx":
                    if isinstance(v, list) and 0 <= arg < len(v):
                        nxt.append(v[arg])
                elif kind == "star":
                    if isinstance(v, list):
                        nxt.extend(v)
            current = nxt
            if not current:
                return []
        return current
