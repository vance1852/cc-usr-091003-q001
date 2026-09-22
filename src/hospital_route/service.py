"""调度服务门面：组合日志存储与调度引擎，提供值班操作入口。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import ContractError, EventEnvelope
from .engine import DispatchEngine
from .layout import Layout
from .rules import Rules
from .store import Journal


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class DispatchService:
    """可独立运行的配送调度后端。

    用法::

        service = DispatchService.open("var/state")
        service.adopt_layout(layout_dict)
        service.adopt_rules(rules_dict)
        report = service.ingest(envelopes)
        view = service.view()
    """

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.layout: Layout | None = None
        self.rules: Rules | None = None
        self.engine: DispatchEngine | None = None
        self._replay()

    @classmethod
    def open(cls, directory: str | Path) -> "DispatchService":
        return cls(Journal(directory))

    # ------------------------------------------------------------------
    # 重放
    # ------------------------------------------------------------------

    def _replay(self) -> None:
        for record in self.journal.read_all():
            kind = record.get("record")
            if kind == "layout":
                self.layout = Layout.from_dict(record["data"])
                self._rebuild_engine()
            elif kind == "rules":
                self.rules = Rules.from_dict(record["data"])
                self._rebuild_engine()
            elif kind == "event":
                self._require_engine()
                self.engine.apply(EventEnvelope.from_dict(record["data"]))

    def _rebuild_engine(self) -> None:
        if self.layout is None or self.rules is None:
            return
        if self.engine is None:
            self.engine = DispatchEngine(self.layout, self.rules)
        else:
            # 规则/拓扑更新只影响后续决策，既有任务保留其签认时的规则版本
            self.engine.layout = self.layout
            self.engine.rules = self.rules

    def _require_engine(self) -> DispatchEngine:
        if self.engine is None:
            raise ContractError("尚未采用拓扑与规则，请先 adopt_layout / adopt_rules")
        return self.engine

    # ------------------------------------------------------------------
    # 配置采用
    # ------------------------------------------------------------------

    def adopt_layout(self, raw: dict[str, Any]) -> str:
        layout = Layout.from_dict(raw)
        if self.layout is not None and self.layout.layout_id == layout.layout_id:
            return self.layout.layout_id
        self.journal.append({"record": "layout", "data": raw})
        self.layout = layout
        self._rebuild_engine()
        return layout.layout_id

    def adopt_rules(self, raw: dict[str, Any]) -> str:
        rules = Rules.from_dict(raw)
        if self.rules is not None and self.rules.version == rules.version:
            return self.rules.version
        self.journal.append({"record": "rules", "data": raw})
        self.rules = rules
        self._rebuild_engine()
        return rules.version

    # ------------------------------------------------------------------
    # 事件接入
    # ------------------------------------------------------------------

    def ingest(self, events: Iterable[EventEnvelope]) -> dict[str, Any]:
        """按 (received_at, event_id) 顺序应用事件；重复 event_id 幂等忽略。"""

        engine = self._require_engine()
        ordered = sorted(events, key=lambda e: (e.received_at, e.event_id))
        report: dict[str, Any] = {"applied": 0, "duplicates": 0, "notes": []}
        for event in ordered:
            if event.event_id in engine.applied_event_ids:
                report["duplicates"] += 1
                continue
            notes = engine.apply(event)
            self.journal.append({"record": "event", "data": _envelope_to_dict(event)})
            report["applied"] += 1
            report["notes"].extend(notes)
        return report

    def override(
        self,
        mission_id: str,
        action: str,
        operator: str,
        reason: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """人工接管：恢复或取消暂停任务，操作本身作为事件入日志。"""

        engine = self._require_engine()
        if at is None:
            if engine.now is not None:
                base = datetime.fromisoformat(engine.now)
            else:
                base = datetime.now(timezone.utc)
            at = _iso(base + timedelta(seconds=1))
        seq = 1
        if mission_id in engine.missions:
            seq = len(engine.missions[mission_id].overrides) + 1
        event = EventEnvelope(
            event_id=f"override-{mission_id}-{seq}",
            kind="manual_override",
            occurred_at=at,
            received_at=at,
            attributes={
                "mission_id": mission_id,
                "action": action,
                "operator": operator,
                "reason": reason,
            },
        )
        return self.ingest([event])

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def view(self, at: str | None = None) -> dict[str, Any]:
        return self._require_engine().view(at)

    def delivery_record(self, mission_id: str) -> dict[str, Any]:
        return self._require_engine().delivery_record(mission_id)

    def mission_ids(self) -> list[str]:
        engine = self._require_engine()
        return sorted(engine.missions)


def _envelope_to_dict(event: EventEnvelope) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
        "attributes": dict(event.attributes),
    }
