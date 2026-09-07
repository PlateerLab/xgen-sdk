"""Host adapters — how the XGEN platform plugs the loop into the State Spine.

The platform's `harness_spine_service` satisfies `SpineStore` with a thin
wrapper (append -> write_spine, query -> read_spine). Everything here is
row-dict based so the engine keeps zero knowledge of the DB layer.

Row conventions (mirrors xgen_harness_spine):
- spine_type "activity"       payload {type,name,ok,detail,run_id}
- spine_type "lesson"         payload {text,run_id}
- spine_type "refined_memory" payload {text,run_id}
- spine_type "judge_score"    payload {score,run_id}
- spine_type "skill_def"      payload {event: commit|status|outcome, ...}
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Protocol

from .ledger import InMemorySkillLedger, SkillRecord
from .model import Provenance, RunTrace, SkillDef, TraceEvent


class SpineStore(Protocol):
    def append(self, spine_type: str, key_id: str, payload: dict) -> None: ...
    def query(self, spine_type: str,
              key_id: str | None = None) -> list[dict]: ...


class InMemorySpineStore:
    def __init__(self) -> None:
        self._rows: list[tuple[str, str, dict]] = []

    def append(self, spine_type: str, key_id: str, payload: dict) -> None:
        self._rows.append((spine_type, key_id, dict(payload)))

    def query(self, spine_type: str, key_id: str | None = None) -> list[dict]:
        return [dict(p) for t, k, p in self._rows
                if t == spine_type and (key_id is None or k == key_id)]


def trace_from_spine(store: SpineStore, run_id: str,
                     scope: str = "user", scope_key: str = "") -> RunTrace:
    events = [
        TraceEvent(
            type=row.get("type", "note"),
            name=row.get("name", ""),
            ok=bool(row.get("ok", True)),
            detail=row.get("detail", ""),
        )
        for row in store.query("activity", run_id)
    ]
    lessons = [row.get("text", "") for row in store.query("lesson", run_id)]
    refined = "\n".join(row.get("text", "")
                        for row in store.query("refined_memory", run_id))
    judge_rows = store.query("judge_score", run_id)
    judge = float(judge_rows[-1]["score"]) if judge_rows else None
    outcome_ok = all(e.ok for e in events if e.type == "outcome") if events else True
    return RunTrace(
        run_id=run_id,
        scope=scope,
        scope_key=scope_key,
        events=events,
        lessons=[l for l in lessons if l],
        refined_memory=refined,
        judge_score=judge,
        success=outcome_ok,
    )


def signature_counts(traces: Iterable[RunTrace]) -> dict[str, int]:
    """Historical tool-sequence counts feeding the repetition signal."""
    return dict(Counter(t.signature() for t in traces))


class SpineSkillLedger(InMemorySkillLedger):
    """SkillLedger persisted as append-only `skill_def` spine rows.

    Same fold discipline as JsonlSkillLedger; the spine's own versioning and
    provenance columns come for free on the platform side.
    """

    SPINE_TYPE = "skill_def"

    def __init__(self, store: SpineStore) -> None:
        super().__init__()
        self.store = store
        self._replay()

    def _replay(self) -> None:
        for row in self.store.query(self.SPINE_TYPE):
            event = row.get("event")
            if event == "commit":
                data = dict(row["skill"])
                provenance = data.pop("provenance", None)
                skill = SkillDef(**{**data, "provenance": None})
                if provenance:
                    skill.provenance = Provenance(**provenance)
                super().commit(skill, note=row.get("note", ""))
                record = super().get(skill.name)
                if record is not None:
                    record.skill.version = data.get("version", record.skill.version)
            elif event == "status":
                super().set_status(row["name"], row["status"], row.get("note", ""))
            elif event == "outcome":
                super().record_outcome([row["name"]], row["success"])

    def commit(self, skill: SkillDef, note: str = "") -> SkillRecord:
        record = super().commit(skill, note)
        self.store.append(self.SPINE_TYPE, skill.name,
                          {"event": "commit", "skill": record.skill.to_dict(),
                           "note": note})
        return record

    def set_status(self, name: str, status: str, note: str = "") -> SkillRecord:
        record = super().set_status(name, status, note)
        self.store.append(self.SPINE_TYPE, name,
                          {"event": "status", "name": name, "status": status,
                           "note": note})
        return record

    def record_outcome(self, names, success: bool) -> None:
        names = list(names)
        super().record_outcome(names, success)
        for name in names:
            if super().get(name) is not None:
                self.store.append(self.SPINE_TYPE, name,
                                  {"event": "outcome", "name": name,
                                   "success": success})
