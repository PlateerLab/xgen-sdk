"""memory/recall — Recall Workspace (작업기억 보존소) 자료구조."""

from xgen_sdk.harness.memory.recall import (
    DEFAULT_RECALL_CAP,
    Priority,
    RecallItem,
    RecallSet,
)


def test_priority_rank_order():
    assert Priority.CRITICAL.rank > Priority.HIGH.rank > Priority.NORMAL.rank > Priority.LOW.rank


def test_priority_coerce():
    assert Priority.coerce("critical") is Priority.CRITICAL
    assert Priority.coerce(Priority.LOW) is Priority.LOW
    assert Priority.coerce("nonsense") is Priority.NORMAL
    assert Priority.coerce(None) is Priority.NORMAL


def test_keep_adds_item():
    rs = RecallSet()
    it = rs.keep(id="d1", content="solar panels degrade ~0.5%/yr", source="report.pdf",
                 priority="high", score=0.9, turn=1)
    assert it.priority is Priority.HIGH
    assert len(rs.items) == 1
    assert rs.get("d1").source == "report.pdf"


def test_keep_dedup_by_id_updates():
    rs = RecallSet()
    rs.keep(id="d1", content="v1")
    rs.keep(id="d1", content="v2 longer body", priority="critical")
    assert len(rs.items) == 1
    assert rs.get("d1").content == "v2 longer body"
    assert rs.get("d1").priority is Priority.CRITICAL


def test_keep_dedup_by_fingerprint_even_with_new_id():
    rs = RecallSet()
    rs.keep(id="a", content="Same Body Here")
    rs.keep(id="b", content="same   body here")  # 같은 지문 → 갱신, 신규 추가 X
    assert len(rs.items) == 1


def test_priority_upgrade_only():
    rs = RecallSet()
    rs.keep(id="d1", content="x", priority="critical")
    rs.keep(id="d1", content="x", priority="low")  # downgrade 무시
    assert rs.get("d1").priority is Priority.CRITICAL


def test_cap_evicts_lowest_priority_first():
    rs = RecallSet(cap=2)
    rs.keep(id="low", content="a", priority="low", turn=1)
    rs.keep(id="hi", content="b", priority="critical", turn=2)
    rs.keep(id="mid", content="c", priority="high", turn=3)
    ids = {i.id for i in rs.items}
    assert len(rs.items) == 2
    assert ids == {"hi", "mid"}  # 최저 우선순위(low) eviction


def test_check_records_note():
    rs = RecallSet()
    rs.keep(id="d1", content="claim")
    assert rs.check("d1", True, "checked against source") is not None
    assert rs.get("d1").checked is True
    assert rs.get("d1").note == "checked against source"
    assert rs.check("missing", True) is None


def test_discard():
    rs = RecallSet()
    rs.keep(id="d1", content="x")
    assert rs.discard("d1") is True
    assert rs.discard("d1") is False
    assert rs.items == []


def test_ranked_order():
    rs = RecallSet()
    rs.keep(id="a", content="a", priority="low", score=0.1, turn=1)
    rs.keep(id="b", content="b", priority="critical", score=0.2, turn=2)
    rs.keep(id="c", content="c", priority="high", score=0.9, turn=3)
    assert [i.id for i in rs.ranked()] == ["b", "c", "a"]


def test_render_compact_and_budget():
    rs = RecallSet()
    for i in range(5):
        rs.keep(id=f"d{i}", content=f"body number {i} " * 10, priority="high", turn=i)
    full = rs.render()
    assert "recall workspace" in full
    assert full.count("\n- ") == 5
    budgeted = rs.render(max_chars=120)
    assert "budget reached" in budgeted


def test_render_empty():
    assert RecallSet().render() == "[recall workspace: empty]"


def test_roundtrip_dict_and_json():
    rs = RecallSet(cap=7)
    rs.keep(id="d1", content="hello", source="s", priority="critical", score=0.5, turn=2)
    rs.check("d1", False, "stale")
    again = RecallSet.from_dict(rs.to_dict())
    assert again.cap == 7
    assert again.get("d1").priority is Priority.CRITICAL
    assert again.get("d1").checked is False
    assert again.get("d1").note == "stale"
    again2 = RecallSet.from_json(rs.to_json())
    assert again2.get("d1").content == "hello"


def test_default_cap_constant():
    assert RecallSet().cap == DEFAULT_RECALL_CAP


def test_item_roundtrip():
    it = RecallItem(id="x", content="c", priority=Priority.HIGH, checked=True)
    assert RecallItem.from_dict(it.to_dict()).checked is True
