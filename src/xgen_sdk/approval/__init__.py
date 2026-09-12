"""사내 결재 — 결재선을 따라 사람이 승인/거절하는 체계.

    engine.py    규칙(상태기계). DB 를 모른다 — 전부 dict 위에서 시험된다.
    store.py     원장 읽기/쓰기. 규칙은 여기 두지 않는다.
    catalog.py   결재를 태울 수 있는 **행위 목록** — 세 레포가 같은 것을 본다.
    policy.py    그중 무엇이 지금 결재 필수인가. **기본은 아무것도 막지 않는다.**
    registry.py  승인됐을 때 무엇을 할지. 훅은 행위를 가진 서비스가 꽂는다.
    directory.py 결재자를 찾는 길 — 부서(역할)는 필터일 뿐이다.
    notifier.py  기존 알림 체계(``user_notifications``)를 그대로 탄다.
    models.py    표 정의(DDL). 표를 만드는 것은 core 한 곳이다.
    sql.py       DB 와 말하는 유일한 통로.

왜 SDK 에 있나
--------------
결재를 올리는 쪽(배포·지식·클라우드·DB·도구 스토어)은 xgen-core 가 아니라
**xgen-workflow / xgen-documents** 에 있다. 결재 원장은 하나여야 하므로 셋 중
누군가는 남의 표에 써야 하는데, 이 집의 방식은 그것을 HTTP 로 엮는 게 아니라
**같은 코드·같은 표**를 공유하는 것이다(``deploy_state_machine``,
``user_notifications`` 가 이미 그렇게 산다). 서비스 간 호출로 엮으면 배포 순서가
의존 방향을 타고, core 가 내려가 있는 동안 결재를 올릴 수 없게 된다.

역할 분담
---------
  * **표를 만드는 것**은 core 만 한다 (``models.py`` 를 APPLICATION_MODELS 에 등록).
  * **결재를 올리는 것**(``store.submit``)은 어느 서비스에서든 한다.
  * **결정**(``store.decide``)은 core 의 ``/api/approval`` 한 곳에서만 한다 —
    승인 순간의 판정과 알림이 두 곳에 살면 어긋난다.
"""
from xgen_sdk.approval import (  # noqa: F401
    catalog, directory, engine, models, notifier, policy, registry, sql, store,
)
from xgen_sdk.approval.catalog import ActionSpec, CATALOG  # noqa: F401
from xgen_sdk.approval.engine import ApprovalError, ApprovalLineRequired  # noqa: F401
from xgen_sdk.approval.models import (  # noqa: F401
    ApprovalActionPolicy,
    ApprovalLine,
    ApprovalLineStep,
    ApprovalPolicyHistory,
    ApprovalRequest,
    ApprovalRequestStep,
)
from xgen_sdk.approval.registry import (  # noqa: F401
    GENERIC,
    TEST,
    is_registered,
    is_user_submittable,
    known_actions,
    register_action,
    run_apply,
    run_reject,
    submittable_actions,
)

__all__ = [
    "catalog", "directory", "engine", "models", "notifier", "policy", "registry",
    "sql", "store",
    "ActionSpec", "CATALOG", "ApprovalError", "ApprovalLineRequired",
    "ApprovalActionPolicy", "ApprovalLine", "ApprovalLineStep",
    "ApprovalPolicyHistory", "ApprovalRequest", "ApprovalRequestStep",
    "GENERIC", "TEST", "is_registered", "is_user_submittable", "known_actions",
    "register_action", "run_apply", "run_reject", "submittable_actions",
]
