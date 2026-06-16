"""builtin recall 툴 (keep/check/recall) — state + pd_stores 배선."""

from xgen_sdk.harness.core.state import PipelineState
from xgen_sdk.harness.memory.recall import RecallSet
from xgen_sdk.harness.tools.builtin import (
    RECALL_PD_KIND,
    CheckTool,
    KeepTool,
    RecallTool,
)


async def test_keep_stores_in_state_and_pd():
    st = PipelineState()
    res = await KeepTool(st).execute(
        {"id": "d1", "content": "solar 0.5%/yr", "priority": "high", "source": "r.pdf"}
    )
    assert not res.is_error
    rs = st.metadata["recall_set"]
    assert isinstance(rs, RecallSet)
    assert rs.get("d1").priority.value == "high"
    # PD step-in: 전체 본문은 pd_stores["recall"] 로 보존
    entry = st.pd_fetch(RECALL_PD_KIND, "d1")
    assert entry is not None and entry["full"] == "solar 0.5%/yr"


async def test_keep_requires_id_and_content():
    st = PipelineState()
    assert (await KeepTool(st).execute({"id": "", "content": "x"})).is_error
    assert (await KeepTool(st).execute({"id": "a", "content": "  "})).is_error


async def test_check_records_and_missing_errors():
    st = PipelineState()
    await KeepTool(st).execute({"id": "d1", "content": "claim"})
    res = await CheckTool(st).execute({"id": "d1", "ok": False, "note": "stale"})
    assert not res.is_error
    assert st.metadata["recall_set"].get("d1").checked is False
    assert (await CheckTool(st).execute({"id": "zzz", "ok": True})).is_error


async def test_recall_compact_view():
    st = PipelineState()
    await KeepTool(st).execute({"id": "d1", "content": "a", "priority": "critical"})
    await KeepTool(st).execute({"id": "d2", "content": "b", "priority": "low"})
    res = await RecallTool(st).execute({})
    assert not res.is_error
    assert "recall workspace" in res.content
    assert "d1" in res.content and "d2" in res.content


async def test_cap_read_from_config():
    class Cfg:
        recall_cap = 1

    st = PipelineState(config=Cfg())
    await KeepTool(st).execute({"id": "d1", "content": "a", "priority": "low"})
    await KeepTool(st).execute({"id": "d2", "content": "b", "priority": "high"})
    rs = st.metadata["recall_set"]
    assert rs.cap == 1
    assert len(rs.items) == 1 and rs.get("d2") is not None  # 최저 우선순위 eviction
