"""s06_context — Intent Routing Mixin (v1.0 흡수 from 구 s05_strategy).

`ContextStage` 의 intent → metadata_filter 자동 결정 로직을 본 모듈로 분리.
사용자 stage_param `intent_rules` 데이터로만 결정 — 코드 박제 0.

확장 패턴:
  - 새 매칭 알고리즘이 필요하면 본 mixin 의 `_apply_intent_routing` 만 override.
  - 키워드/필터 정의 자체는 사용자 데이터 (stage_param) 영역.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.context.intent")


class IntentRoutingMixin:
    async def _apply_intent_routing(self, state: "PipelineState") -> None:
        """stage_params.intent_rules 로 user_input 분류 → state.metadata["auto_metadata_filter"].

        rules 구조 (UI textarea / stage_param 양쪽 허용):
          [{"keywords": ["상품", "product"], "filter": {"file_name": "products.csv"}},
           {"keywords": ["리뷰", "review"],  "filter": {"file_name": "reviews.csv"}}]

        첫 매칭 rule 의 filter 를 auto_metadata_filter 에 저장.
        s06 본 execute 가 stage_params.metadata_filter 가 비어있을 때만 이 값 사용.
        키워드/필터 정의는 사용자 stage_param 데이터로만 결정 — 코드에 박제 0.
        """
        rules = self.get_param("intent_rules", state, None)
        # UI textarea 로 오면 JSON 문자열. 파싱.
        if isinstance(rules, str) and rules.strip():
            try:
                import json as _json
                rules = _json.loads(rules)
            except Exception as e:
                logger.debug("[Context] intent_rules JSON 파싱 실패: %s", e)
                rules = None
        if not rules or not isinstance(rules, list):
            return
        user_input = (state.user_input or "").lower()
        if not user_input:
            return
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            keywords = rule.get("keywords") or []
            if not isinstance(keywords, list) or not keywords:
                continue
            if any(str(k).lower() in user_input for k in keywords):
                filt = rule.get("filter")
                if isinstance(filt, dict) and filt:
                    state.metadata["auto_metadata_filter"] = filt
                    logger.info(
                        "[Context] intent_routing matched keywords=%s → auto_metadata_filter=%s",
                        keywords, filt,
                    )
                    return
