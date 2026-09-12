"""유일 인덱스는 성능 장치가 아니라 **불변의 집행**이다.

왜 생겼나 (2026-09-09 사고의 후속)
-----------------------------------
`deploy_meta` 는 "workflow_id 하나에 행 하나" 라는 규칙을 애플리케이션 코드로만
지키고 있었다. 그 규칙이 한 번 깨지자(소유자를 클라이언트에게 물어본 저장 경로)
같은 workflow_id 에 행이 둘 생겼고, 배포 여부를 읽는 곳들이 엉뚱한 행을 집어
살아 있는 배포가 403 이 됐다.

코드만 지키는 규칙은 다른 경로가 조용히 깨뜨린다. DB 가 거절해야 규칙이 된다.

그런데 인덱스 생성은 실패를 `warning` 으로 **삼키고** 있었다. 그 상태에서 유일
인덱스를 선언하면 "있다고 믿는데 실제로는 없는 제약" 이 된다 — 제약이 아예 없는
것보다 나쁘다. 그래서 두 가지를 함께 고정한다: 유일 인덱스를 만들 수 있어야 하고,
못 만들었으면 **소리가 나야** 한다.
"""

import logging

from xgen_sdk.db.base_model import BaseModel


class _Model(BaseModel):
    """인덱스 정의만 있는 최소 모델."""

    def get_table_name(self):
        return "deploy_meta"

    def get_schema(self):
        return {"workflow_id": "VARCHAR(100) NOT NULL"}

    def get_indexes(self):
        return [
            ("idx_plain", "user_id"),
            ("uq_workflow", "workflow_id", True),
        ]


def _build_queries(model, executed, failing=()):
    """app_db 의 인덱스 생성 루프와 **같은 규칙**으로 질의를 만든다.

    실제 루프를 직접 돌리려면 DB 연결이 필요하다. 여기서는 규칙(세 번째 원소가
    True 면 UNIQUE)이 지켜지는지와, 실패가 어떻게 다뤄지는지를 본다.
    """
    unenforced = []
    for index_def in model.get_indexes():
        name, columns = index_def[0], index_def[1]
        unique = bool(index_def[2]) if len(index_def) > 2 else False
        kind = "UNIQUE INDEX" if unique else "INDEX"
        query = f"CREATE {kind} IF NOT EXISTS {name} ON {model.get_table_name()}({columns})"
        if name in failing:
            if unique:
                unenforced.append({"table": model.get_table_name(), "index": name,
                                   "columns": columns, "reason": "duplicate key"})
            continue
        executed.append(query)
    return unenforced


def test_third_element_makes_the_index_unique():
    executed = []
    _build_queries(_Model(), executed)
    assert any("CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow" in q for q in executed)
    assert any("CREATE INDEX IF NOT EXISTS idx_plain" in q for q in executed)
    assert not any("UNIQUE" in q and "idx_plain" in q for q in executed), (
        "세 번째 원소가 없는 인덱스까지 유일해지면 정상 중복이 막힌다"
    )


def test_a_unique_index_that_failed_is_reported_not_swallowed():
    """못 만든 유일 인덱스는 목록에 남는다 — 조용히 넘어가지 않는다."""
    unenforced = _build_queries(_Model(), [], failing=("uq_workflow",))
    assert len(unenforced) == 1
    assert unenforced[0]["table"] == "deploy_meta"
    assert unenforced[0]["columns"] == "workflow_id"
    assert "duplicate" in unenforced[0]["reason"]


def test_a_plain_index_that_failed_is_not_reported_as_unenforced():
    """일반 인덱스는 성능 장치라 실패해도 불변이 깨지지 않는다 — 구분한다."""
    assert _build_queries(_Model(), [], failing=("idx_plain",)) == []


def test_app_db_wires_unique_indexes_and_reports_failures():
    """실제 배선 확인 — 규칙이 app_db 안에 있는지.

    이 배선이 빠지면 모델이 유일을 선언해도 일반 인덱스로 만들어지고, 불변은
    집행되지 않은 채 아무 신호도 나지 않는다.
    """
    import inspect

    from xgen_sdk.db import app_db as app_db_module

    src = inspect.getsource(app_db_module.XgenDB.create_tables)
    assert "CREATE {kind} IF NOT EXISTS" in src, "유일/일반을 가르는 배선이 없다"
    assert "if unique:" in src, "유일 인덱스 실패를 따로 다루는 분기가 없다"
    assert "_unenforced_unique_indexes.append" in src, (
        "못 만든 유일 인덱스를 값으로 남기지 않는다 — 조용한 실패가 된다"
    )
    # 바깥 except 에도 logger.error 가 있으므로 '어딘가에 error 가 있다' 는
    # 공허하다. 이 분기의 **문구**로 못박는다.
    assert "유일 제약을 만들지 못했습니다" in src, (
        "유일 인덱스 실패를 warning 으로 삼키면 '있다고 믿는데 없는 제약' 이 된다"
    )


def test_accessor_exists_so_operations_can_ask():
    from xgen_sdk.db.app_db import XgenDB

    assert hasattr(XgenDB, "unenforced_unique_indexes")
