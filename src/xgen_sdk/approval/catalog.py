"""XGEN 에서 **결재를 태울 수 있는 행위들** — 하나의 선언.

왜 한 곳에 모으나
-----------------
행위를 실제로 여는 코드는 세 레포에 흩어져 있다(배포는 workflow, 지식은
documents, 클라우드·DB·도구는 workflow). 그런데 관리자가 "무엇을 결재 필수로
할까" 를 고르는 화면은 core 에 있다. 목록을 각자 갖고 있으면 core 의 화면은
자기가 아는 것만 보여 주고, **화면에 없는 행위는 영영 켤 수 없다** — 그리고
아무도 그 사실을 모른다(오류가 안 난다).

그래서 목록은 SDK 에 선언하고, 세 레포가 같은 것을 본다. 여기에 없는
``action_type`` 은 결재로 올라가지 않는다.

무엇이 여기 없나
----------------
사용자가 마이페이지에서 직접 올리는 자유 결재(``generic``·``test``)는 **게이트가
아니다**. 관리자가 켜고 끌 대상이 아니므로 [결재 목록 설정] 에 나오지 않는다.
반대로 게이트 행위는 사용자가 마이페이지에서 직접 올릴 수 없다 — payload 를
손으로 적어 올리면 남의 워크플로우를 배포시키는 길이 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: apply 를 실행하는 서비스. 결재 원장은 하나지만 **적용은 행위를 가진 쪽**이
#: 한다 — core 가 남의 파드 안 함수를 부를 수는 없기 때문이다.
OWNER_CORE = "core"
OWNER_WORKFLOW = "workflow"

#: 화면에서 묶어 보여 줄 갈래.
DOMAIN_DEPLOY = "배포"
DOMAIN_KNOWLEDGE = "지식"
DOMAIN_CLOUD = "클라우드"
DOMAIN_DB = "DB"
DOMAIN_TOOL = "도구"
DOMAIN_FREE = "일반"


@dataclass(frozen=True)
class ActionSpec:
    """결재를 태울 수 있는 행위 하나."""

    action_type: str
    label: str
    description: str
    owner: str
    domain: str
    #: [결재 목록 설정] 에서 켜고 끄는 대상인가.
    gated: bool = True
    #: 사람이 마이페이지에서 **직접** 올릴 수 있는가.
    user_submittable: bool = False
    #: 이 행위를 하는 화면이 **결재선을 고를 자리**를 갖고 있는가.
    #:
    #: 배포는 모달이 있어서 사용자가 결재자를 고른다. 지식 컬렉션 생성이나 도구
    #: 게시는 버튼 하나다 — 거기서 결재선을 물을 자리가 없다. 그런 행위를
    #: 기본 결재선 없이 켜면 결재가 **아무에게도 가지 않고** 사용자는 그냥
    #: "실패했습니다" 만 본다. 그래서 정책이 켜질 때 기본 결재선을 요구한다.
    picks_line: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "action_type": self.action_type,
            "label": self.label,
            "description": self.description,
            "owner": self.owner,
            "domain": self.domain,
            "gated": self.gated,
            "user_submittable": self.user_submittable,
            "picks_line": self.picks_line,
        }


GENERIC = "generic"
TEST = "test"

#: 에이전트 배포. 지금까지 "관리자 승인 → 거버넌스 심사" 2단계였던 그 자리다.
AGENT_DEPLOY = "agent.deploy"

CATALOG: Tuple[ActionSpec, ...] = (
    # ── 자유 결재 — 게이트가 아니다 ──
    ActionSpec(
        GENERIC, "일반 결재", "결재 자체가 결론인 건 (적용 동작 없음)",
        OWNER_CORE, DOMAIN_FREE, gated=False, user_submittable=True,
    ),
    ActionSpec(
        TEST, "테스트 결재", "결재선이 도는지 확인하는 건 (아무것도 바꾸지 않는다)",
        OWNER_CORE, DOMAIN_FREE, gated=False, user_submittable=True,
    ),

    # ── 배포 ──
    ActionSpec(
        AGENT_DEPLOY, "에이전트 배포",
        "에이전트를 외부(URL·임베드·API)로 여는 것",
        OWNER_CORE, DOMAIN_DEPLOY, picks_line=True,
    ),

    # ── 지식 ──
    ActionSpec(
        "collection.create", "지식 컬렉션 생성",
        "새 지식 컬렉션을 만들어 검색에 쓰는 것",
        OWNER_WORKFLOW, DOMAIN_KNOWLEDGE,
    ),
    ActionSpec(
        "collection.upload", "지식 문서 업로드",
        "컬렉션에 문서를 올려 검색 대상에 넣는 것",
        OWNER_WORKFLOW, DOMAIN_KNOWLEDGE,
    ),
    ActionSpec(
        "collection.update", "지식 문서 갱신",
        "이미 올린 문서를 새 판으로 바꾸는 것",
        OWNER_WORKFLOW, DOMAIN_KNOWLEDGE,
    ),
    ActionSpec(
        "filestore.embed", "파일 저장소 임베딩",
        "파일 저장소의 파일을 검색 색인에 넣는 것",
        OWNER_WORKFLOW, DOMAIN_KNOWLEDGE,
    ),

    # ── 클라우드 ──
    ActionSpec(
        "cloud.storage_create", "클라우드 저장소 생성",
        "내 클라우드에 새 저장소(루트 폴더)를 만드는 것",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),
    ActionSpec(
        "cloud.upload", "클라우드 업로드",
        "내 클라우드에 파일을 올리는 것",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),
    ActionSpec(
        "cloud.update", "클라우드 파일 갱신",
        "내 클라우드의 파일을 새 판으로 바꾸는 것",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),
    ActionSpec(
        "cloud.share", "클라우드 공유",
        "내 클라우드의 폴더·파일을 다른 사람에게 여는 것",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),
    ActionSpec(
        "cloud.device_link", "PC 연결",
        "접속기를 깐 PC 를 계정에 연결하는 것 (그 PC 와 파일이 오간다)",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),
    ActionSpec(
        "cloud.agent_link", "에이전트 연결",
        "에이전트가 내 클라우드 저장소를 쓰도록 잇는 것",
        OWNER_WORKFLOW, DOMAIN_CLOUD,
    ),

    # ── DB ──
    ActionSpec(
        "db.create", "DB 연결 생성",
        "외부 데이터베이스 연결을 만들어 쓰는 것",
        OWNER_WORKFLOW, DOMAIN_DB,
    ),
    ActionSpec(
        "db.share", "DB 연결 공유",
        "내 DB 연결을 다른 사람에게 여는 것",
        OWNER_WORKFLOW, DOMAIN_DB,
    ),

    # ── 도구 ──
    ActionSpec(
        "tool.publish", "도구 스토어 게시",
        "에이전트가 만든 도구를 전체 사용자에게 게시하는 것 "
        "(임의 코드가 조직 전체의 샌드박스에서 돌 수 있는 길이다)",
        OWNER_WORKFLOW, DOMAIN_TOOL,
    ),
)

_BY_TYPE: Dict[str, ActionSpec] = {s.action_type: s for s in CATALOG}


def spec(action_type: str) -> Optional[ActionSpec]:
    return _BY_TYPE.get(str(action_type or ""))


def gated_specs() -> Tuple[ActionSpec, ...]:
    """[결재 목록 설정] 이 보여 주는 것 — 켜고 끌 수 있는 행위들."""
    return tuple(s for s in CATALOG if s.gated)


def owned_by(owner: str) -> Tuple[str, ...]:
    """그 서비스가 **적용**을 책임지는 행위들. apply 워커가 이 목록을 본다."""
    return tuple(s.action_type for s in CATALOG if s.owner == owner)
