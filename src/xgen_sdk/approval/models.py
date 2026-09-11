"""사내 결재(approval) — 결재선·결재 문서·단계 원장.

무엇을 푸는가
-------------
"이 작업은 누구누구의 승인을 받아야 한다" 를 **작업과 분리해서** 다룬다.
어떤 API 에 결재를 태울지는 나중에 정하고(:mod:`xgen_sdk.approval.registry`
의 action 레지스트리), 여기서는 그 결정과 무관하게 성립하는 것만 담는다:
누가 올렸고, 누구를 거쳐, 지금 누구 차례이고, 무엇으로 끝났는가.

기존 :class:`RagApprovalRequest` 와 무엇이 다른가
-------------------------------------------------
그쪽은 **"관리자 아무나 한 명"** 이 승인하는 1단 게이트다(RAG 자산 통제 전용).
이쪽은 **지정된 사람들이 정해진 순서로** 처리하는 결재선이다. 상태 축도
다르고(단계마다 차례가 있다) 조회 축도 다르다(내게 올라온 것). 한 테이블에
욱여넣으면 양쪽 질의가 서로를 방해하므로 따로 둔다.

결재선은 한 줄이다
-------------------
``[A] → [B] → [C]``. ``step_order`` 는 1 부터 빈틈없이, **한 차례에 한 사람**
이다(표의 UNIQUE 가 그것을 강제한다). A 가 승인해야 B 에게 가고, 마지막
사람의 승인이 곧 최종 승인이다.

스냅샷 원칙
-----------
``approval_request_steps`` 는 템플릿의 **복사본**이다. 참조가 아니다.
진행 중인 결재의 결재선이 누군가의 템플릿 수정으로 바뀌면, 이미 승인한
사람이 승인하지 않은 사람이 되거나 그 반대가 된다 — 결재에서 그건 위조다.
"""
from typing import Dict, List

from xgen_sdk.db.base_model import BaseModel


class ApprovalLine(BaseModel):
    """결재선 템플릿 — "이 종류의 일은 이 사람들을 거친다".

    템플릿 없이 그때그때 사람을 지정하는 것도 된다(요청이 단계를 직접 들고
    온다). 템플릿은 **같은 줄을 반복해서 쓸 때** 손이 덜 가라고 있는 것이지
    결재의 전제가 아니다.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = kwargs.get('name', '')
        self.description = kwargs.get('description')
        self.owner_id = kwargs.get('owner_id')
        #: 공용 결재선인가. 개인이 만든 줄은 자기만 쓰고, 공용은 모두가 쓴다.
        self.is_shared = kwargs.get('is_shared', False)
        self.is_active = kwargs.get('is_active', True)

    def get_table_name(self) -> str:
        return "approval_lines"

    def get_schema(self) -> Dict[str, str]:
        return {
            'name': 'VARCHAR(100) NOT NULL',
            'description': 'VARCHAR(500)',
            'owner_id': 'INTEGER REFERENCES users(id) ON DELETE SET NULL',
            'is_shared': 'BOOLEAN NOT NULL DEFAULT FALSE',
            'is_active': 'BOOLEAN NOT NULL DEFAULT TRUE',
        }

    def get_indexes(self) -> List[tuple]:
        return [
            ("idx_approval_lines_owner", "owner_id"),
            ("idx_approval_lines_shared", "is_shared, is_active"),
        ]


class ApprovalLineStep(BaseModel):
    """템플릿의 한 칸 — **한 차례에 한 사람**."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.line_id = kwargs.get('line_id')
        self.step_order = kwargs.get('step_order', 1)
        self.approver_id = kwargs.get('approver_id')

    def get_table_name(self) -> str:
        return "approval_line_steps"

    def get_schema(self) -> Dict[str, str]:
        return {
            'line_id': 'INTEGER NOT NULL REFERENCES approval_lines(id) ON DELETE CASCADE',
            'step_order': 'INTEGER NOT NULL DEFAULT 1',
            # 이쪽은 **설정**이라 CASCADE 가 맞다 — 퇴사한 사람은 앞으로 쓸
            # 결재선에서 빠져야 한다. 이미 올라간 결재는 스냅샷이라 영향 없다.
            'approver_id': 'INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE',
            # 같은 줄에 같은 사람을 두 번 세우지 않는다 — 두 번 승인하라는 뜻이
            # 되는데, 그런 결재선은 실수이지 의도인 적이 없다.
            'UNIQUE_line_approver': 'UNIQUE(line_id, approver_id)',
            # **한 차례에 한 사람.** 결재선은 한 줄이라, 같은 번호에 둘이 서면
            # 그건 줄이 아니라 갈래다.
            'UNIQUE_line_order': 'UNIQUE(line_id, step_order)',
        }

    def get_indexes(self) -> List[tuple]:
        return [
            ("idx_approval_line_steps_line", "line_id, step_order"),
        ]


class ApprovalRequest(BaseModel):
    """결재 문서 1건.

    ``action_type`` 은 승인됐을 때 **무엇을 할지** 가리키는 레지스트리 키다.
    지금은 어떤 API 도 결재를 타지 않으므로 대부분 ``generic``(하는 일 없음,
    승인 자체가 결론)이다. 나중에 태울 API 가 정해지면 그 키를 등록하고
    ``payload`` 스키마는 그 applier 가 소유한다.

    상태기계::

        pending ─┬─> approved   (모든 단계 승인 + applier 실행)
                 ├─> rejected   (한 명이라도 거절 — 즉시 종결)
                 └─> canceled   (기안자가 회수)

    종결 상태에서 되돌아오는 전이는 없다. 다시 하려면 새 건을 올린다 —
    이미 승인한 사람의 의사를 나중에 바꿔 쓰는 길을 만들지 않는다.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = kwargs.get('title', '')
        self.reason = kwargs.get('reason')
        self.action_type = kwargs.get('action_type', 'generic')
        self.payload = kwargs.get('payload')
        self.requester_id = kwargs.get('requester_id')
        #: 어느 템플릿에서 왔는지(참고용). 단계는 스냅샷이라 이 값이 바뀌거나
        #: 템플릿이 지워져도 진행 중인 결재는 영향을 받지 않는다.
        self.line_id = kwargs.get('line_id')
        self.status = kwargs.get('status', 'pending')
        #: 지금 차례인 단계 번호(= 그 한 사람). 종결되면 마지막 값에서 멈춘다.
        self.current_step_order = kwargs.get('current_step_order', 1)
        self.decided_at = kwargs.get('decided_at')
        #: 이 결재가 **무엇에 대한** 것인가 — ``workflow:{id}``, ``collection:{name}``.
        #: 같은 대상에 결재가 두 건 떠 있으면 어느 쪽이 적용되는지 아무도 모른다.
        self.target_ref = kwargs.get('target_ref')
        #: 회수한 주체 — ``requester``(사람) 또는 ``system``(대상이 바뀌어 무효).
        self.canceled_by = kwargs.get('canceled_by')
        self.cancel_note = kwargs.get('cancel_note')
        #: applier 실행 결과. 승인은 됐는데 적용이 실패한 상태를 **숨기지 않는다**
        #: — 승인을 되돌리는 것보다 "승인됐으나 적용 실패" 를 보이는 편이 맞다.
        self.applied_at = kwargs.get('applied_at')
        self.apply_error = kwargs.get('apply_error')

    def get_table_name(self) -> str:
        return "approval_requests"

    def get_schema(self) -> Dict[str, str]:
        return {
            'title': 'VARCHAR(200) NOT NULL',
            'reason': 'TEXT',
            'action_type': 'VARCHAR(60) NOT NULL DEFAULT \'generic\'',
            'payload': 'TEXT',
            'requester_id': 'INTEGER REFERENCES users(id) ON DELETE SET NULL',
            'line_id': 'INTEGER REFERENCES approval_lines(id) ON DELETE SET NULL',
            'status': 'VARCHAR(20) NOT NULL DEFAULT \'pending\'',
            'current_step_order': 'INTEGER NOT NULL DEFAULT 1',
            'decided_at': 'TIMESTAMP',
            # 대상 표의 PK 가 아니라 **문자열**이다. 대상은 표마다 다른 키를 쓰고
            # (워크플로우는 문자열 id, 컬렉션은 이름), 대상이 지워져도 "무엇에
            # 대한 결재였나" 는 남아야 한다.
            'target_ref': 'VARCHAR(200)',
            'canceled_by': 'VARCHAR(20)',
            'cancel_note': 'VARCHAR(500)',
            'applied_at': 'TIMESTAMP',
            'apply_error': 'VARCHAR(1000)',
        }

    def get_indexes(self) -> List[tuple]:
        return [
            # "내가 올린 것" — 기안함.
            ("idx_approval_requests_requester", "requester_id, status"),
            ("idx_approval_requests_status", "status"),
            # "이 대상에 뜬 결재" — 배포 화면이 "결재 진행 중" 을 보여 줄 때.
            ("idx_approval_requests_target", "action_type, target_ref"),
        ]

    #: 같은 대상에 **진행 중인 결재는 하나**다. 부분 UNIQUE 는 모델의 인덱스
    #: 선언(``CREATE INDEX``)으로는 못 만든다 — core 의 마이그레이션이 만든다
    #: (``add_approval_policy_tables.py``). 응용 계층도 미리 막지만, 동시 요청
    #: 두 건이 같은 찰나에 들어오면 그것만으로는 늦다.
    PENDING_TARGET_UNIQUE = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending_target "
        "ON approval_requests (action_type, target_ref) "
        "WHERE status = 'pending' AND target_ref IS NOT NULL"
    )


class ApprovalRequestStep(BaseModel):
    """이 건의 결재선 한 칸 — **템플릿의 스냅샷**이다.

    상태::

        waiting  아직 차례가 아니다
        pending  지금 이 사람 차례다      ← 받은 결재함의 질의 축
        approved 승인함
        rejected 거절함 (이 순간 건 전체가 종결)
        skipped  앞에서 거절·회수되어 차례가 오지 않았다

    ``waiting`` 과 ``pending`` 을 나누는 이유: 받은 결재함이
    ``approver_id = 나 AND status = 'pending'`` 한 줄로 끝난다. 합치면 화면이
    매번 "지금 몇 번째 차례인가" 를 계산해야 하고, 그 계산이 두 곳에 생기는
    순간 목록과 상세가 서로 다른 말을 하기 시작한다.

    결재선이 한 줄이므로 ``pending`` 인 단계는 **언제나 정확히 하나**다.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.request_id = kwargs.get('request_id')
        self.step_order = kwargs.get('step_order', 1)
        self.approver_id = kwargs.get('approver_id')
        self.status = kwargs.get('status', 'waiting')
        self.acted_at = kwargs.get('acted_at')
        self.note = kwargs.get('note')

    def get_table_name(self) -> str:
        return "approval_request_steps"

    def get_schema(self) -> Dict[str, str]:
        return {
            'request_id': 'INTEGER NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE',
            'step_order': 'INTEGER NOT NULL DEFAULT 1',
            # ⚠ **users 로의 FK 를 걸지 않는다.** 이 표는 설정이 아니라 **기록**이다.
            #
            # CASCADE 였다면: 사용자를 지우는 순간 그 사람의 단계가 사라진다.
            # 이미 승인한 사람이 결재선에서 없어지고(기록이 거짓이 된다), 하필
            # 그 사람이 지금 차례였다면 그 차수에 아무도 남지 않아 건이 **영영
            # pending 에 멈춘다** — 누구도 이유를 알 수 없는 정지다.
            # (XGEN 은 사용자를 실제로 삭제한다 — adminUserController.delete_user.)
            #
            # RESTRICT 였다면: 결재 이력이 있는 사람을 퇴사 처리할 수 없다.
            #
            # 그래서 FK 를 떼고 id 만 남긴다. 계정이 사라져도 "누가 언제 승인했다"
            # 는 그대로 남고, 이름은 JOIN 이 비면 화면이 #id 로 보여 준다.
            'approver_id': 'INTEGER NOT NULL',
            'status': 'VARCHAR(20) NOT NULL DEFAULT \'waiting\'',
            'acted_at': 'TIMESTAMP',
            'note': 'VARCHAR(500)',
            'UNIQUE_request_approver': 'UNIQUE(request_id, approver_id)',
            # **한 차례에 한 사람** — 표가 강제한다. 이게 없으면 코드 어딘가가
            # 같은 번호에 둘을 넣었을 때 "지금 누구 차례인가" 가 두 답이 된다.
            'UNIQUE_request_order': 'UNIQUE(request_id, step_order)',
        }

    def get_indexes(self) -> List[tuple]:
        return [
            # 받은 결재함 — 이 인덱스 하나가 그 화면의 전부다.
            ("idx_approval_steps_approver_status", "approver_id, status"),
            ("idx_approval_steps_request", "request_id, step_order"),
        ]


class ApprovalActionPolicy(BaseModel):
    """**어떤 행위가 결재를 받아야 하는가** — 관리자가 정하는 한 줄.

    기본은 무제한이다
    -----------------
    표에 행이 없으면 ``결재 불필요``다. 그래서 이 기능이 켜진 환경에서도
    관리자가 켜기 전까지는 XGEN 의 어떤 행위도 막히지 않는다 — 통제를
    도입하면서 아무에게도 묻지 않고 전 조직의 작업을 멈추는 일이 없어야 한다.

    왜 config 가 아니라 표인가
    --------------------------
    행위마다 켜짐 여부 + 기본 결재선 + 누가 언제 바꿨나까지 붙는다. 설정 키
    수십 개로 흩으면 "지금 무엇이 결재 대상인가" 를 한 번에 볼 수 없고,
    화면이 필요로 하는 것이 정확히 그 한 장이다.
    (전역 스위치 하나 — 슈퍼유저 면제 — 만 설정 사다리에 둔다. 그건 행위별
    값이 아니라 전체에 걸리는 하나이기 때문이다.)
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.action_type = kwargs.get('action_type', '')
        self.required = kwargs.get('required', False)
        #: 결재를 올릴 때 **미리 채워지는** 결재선. 요청자가 바꿀 수 있다 —
        #: 잠그지 않는다(무제한 원칙). 없으면 요청자가 직접 고른다.
        self.default_line_id = kwargs.get('default_line_id')
        self.updated_by = kwargs.get('updated_by')
        self.updated_at = kwargs.get('updated_at')

    def get_table_name(self) -> str:
        return "approval_action_policies"

    def get_schema(self) -> Dict[str, str]:
        return {
            # 카탈로그(xgen_sdk.approval.catalog)의 키. FK 가 아니다 —
            # 카탈로그는 코드에 있고, 코드에서 사라진 행위의 옛 설정이 남아
            # 있다고 해서 표가 깨질 이유는 없다.
            'action_type': 'VARCHAR(64) NOT NULL',
            'required': 'BOOLEAN NOT NULL DEFAULT FALSE',
            'default_line_id': 'INTEGER REFERENCES approval_lines(id) ON DELETE SET NULL',
            'updated_by': 'INTEGER',
            'updated_at': 'TIMESTAMP',
            'UNIQUE_action': 'UNIQUE(action_type)',
        }

    def get_indexes(self) -> List[tuple]:
        return [("idx_approval_policies_required", "required")]


class ApprovalPolicyHistory(BaseModel):
    """**누가 언제 무엇을 결재 대상으로 만들었나.**

    [결재 로그] 의 두 번째 축이다. 결재 건의 이력(누가 승인했나)과 설정의
    이력(누가 이 행위를 결재 대상으로 만들었나)은 다른 질문이고, 감사에서
    실제로 문제가 되는 것은 후자일 때가 많다 — "그날 왜 승인 없이 배포됐나" 의
    답이 "전날 누군가 껐다" 인 경우.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        #: 바뀐 대상 — action_type, 또는 전역 스위치 이름(``superuser_exempt``).
        self.target = kwargs.get('target', '')
        self.before = kwargs.get('before')
        self.after = kwargs.get('after')
        self.changed_by = kwargs.get('changed_by')

    def get_table_name(self) -> str:
        return "approval_policy_history"

    def get_schema(self) -> Dict[str, str]:
        return {
            'target': 'VARCHAR(64) NOT NULL',
            'before': 'VARCHAR(300)',
            'after': 'VARCHAR(300)',
            # 기록이므로 users FK 를 걸지 않는다 — 결재 단계와 같은 이유다.
            'changed_by': 'INTEGER',
        }

    def get_indexes(self) -> List[tuple]:
        return [("idx_approval_policy_history_target", "target")]
