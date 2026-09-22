"""配送调度服务外观：装载资料、接收事件、规划、人工接管与持久化。

用法::

    service = HospitalRouteService.load(config_dir)
    service.ingest(events)          # 乱序事件安全重放
    view = service.next_actions()   # 每台机器人下一项合法动作
    service.persist()               # 重启后 load 同一目录即可恢复
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import ContractError, EventEnvelope, load_events
from .engine import SUSPEND_CODES, Engine
from .model import RuleSet, RobotProfile, Topology, load_robots, load_rules, load_topology, validate_robots_against
from .persistence import load_state, save_state
from .report import build_blockers, build_delivery_record, build_next_actions, build_risk_report
from .state import (
    EventProcessor,
    RobotState,
    SystemState,
    TakeoverRecord,
    order_events,
    parse_ts,
)

STATE_FILE = "state.json"


class HospitalRouteService:
    def __init__(
        self,
        topology: Topology,
        rules: RuleSet,
        robots: dict[str, RobotProfile],
        state: SystemState,
        storage_path: Path | None = None,
    ) -> None:
        self.topology = topology
        self.rules = rules
        self.robot_profiles = robots
        self.state = state
        self.storage_path = storage_path
        self.processor = EventProcessor(topology, rules)
        self.engine = Engine(topology, rules, robots, self.processor)

    # ------------------------------------------------------------------
    # 装载与恢复
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_dir: str | Path, scenario: str | None = None) -> "HospitalRouteService":
        """从目录装载 topology.json / rules.json / robots.json；若存在 state.json 则恢复。"""

        base = Path(config_dir)
        topology = load_topology(json.loads((base / "topology.json").read_text(encoding="utf-8")))
        rules = load_rules(json.loads((base / "rules.json").read_text(encoding="utf-8")))
        robots = load_robots(json.loads((base / "robots.json").read_text(encoding="utf-8")))
        validate_robots_against(robots, topology, rules)
        storage = base / STATE_FILE
        if storage.exists():
            state = load_state(storage)
            if state.rule_version != rules.rule_version:
                raise ContractError(
                    f"持久化状态的规则版本 {state.rule_version} 与当前规则 {rules.rule_version} 不一致"
                )
        else:
            state = SystemState(scenario=scenario or base.name, rule_version=rules.rule_version)
            for robot_id, profile in robots.items():
                state.robots[robot_id] = RobotState(
                    robot_id=robot_id, zone=profile.home_zone, updated_at=""
                )
            for zone_id in topology.zones:
                state.zone_contamination.setdefault(zone_id, "clean")
            for elevator_id in topology.elevators:
                state.elevator_contamination.setdefault(elevator_id, "clean")
        return cls(topology, rules, robots, state, storage)

    def persist(self) -> None:
        if self.storage_path is None:
            raise ContractError("服务未配置持久化路径")
        save_state(self.state, self.storage_path)

    # ------------------------------------------------------------------
    # 事件接入
    # ------------------------------------------------------------------

    def ingest(self, events: Iterable[EventEnvelope]) -> dict[str, Any]:
        """接收一批（可能乱序的）事件，按确定性顺序应用。"""

        ordered = order_events(events)
        for event in ordered:
            self.processor.apply(self.state, event)
        return {
            "applied": len(ordered),
            "rejections": [
                {"event_id": r.event_id, "code": r.code, "message": r.message}
                for r in self.state.rejections
            ],
        }

    def ingest_file(self, path: str | Path) -> dict[str, Any]:
        scenario, events = load_events(path)
        self.state.scenario = scenario
        return self.ingest(events)

    # ------------------------------------------------------------------
    # 规划
    # ------------------------------------------------------------------

    def plan_mission(self, mission_id: str, now: datetime | None = None) -> dict[str, Any]:
        """为任务搜索合法路线并登记时段预留；失败返回阻断依据，不绕过。"""

        mission = self.state.missions.get(mission_id)
        if mission is None:
            raise ContractError(f"未知任务 {mission_id}")
        if mission.is_final:
            raise ContractError(f"任务 {mission_id} 已签认为 {mission.status}，不得重新规划")
        if mission.status == "suspended":
            return {
                "mission_id": mission_id,
                "planned": False,
                "reasons": [
                    {
                        "code": reason["code"],
                        "message": reason["message"],
                        "evidence": reason.get("evidence", {}),
                    }
                    for reason in mission.hold_reasons
                ],
            }
        moment = now or self._now()
        candidate, reasons = self.engine.find_route(self.state, mission, moment, ignore_mission=mission_id)
        if candidate is None:
            suspend_hits = [r for r in reasons if r.code in SUSPEND_CODES]
            if suspend_hits:
                # 硬性阻断：进入可人工处置的暂停状态，不另行绕过
                self.processor._mission_hold(
                    self.state,
                    mission,
                    suspend_hits[0].code,
                    suspend_hits[0].message,
                    moment.isoformat(),
                    suspend_hits[0].evidence,
                )
            return {
                "mission_id": mission_id,
                "planned": False,
                "suspended": bool(suspend_hits),
                "reasons": [
                    {"code": r.code, "message": r.message, "evidence": r.evidence} for r in reasons
                ],
            }
        evidence = {
            "rule_version": self.state.rule_version,
            "planned_by": "engine",
            "planned_at": moment.isoformat(),
        }
        reservations = self.engine.commit_plan(self.state, mission, candidate, moment, evidence)
        return {
            "mission_id": mission_id,
            "planned": True,
            "route": [
                {
                    "edge_id": leg.edge_id,
                    "from": leg.from_zone,
                    "to": leg.to_zone,
                    "kind": leg.kind,
                    "elevator_id": leg.elevator_id,
                }
                for leg in candidate.legs
            ],
            "eta": candidate.arrive.isoformat(),
            "reservations": [r.reservation_id for r in reservations],
        }

    def plan_all(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self._now()
        results = []
        for mission_id in sorted(self.state.missions):
            mission = self.state.missions[mission_id]
            if mission.status != "pending":
                continue
            results.append(self.plan_mission(mission_id, moment))
        return {"planned_at": moment.isoformat(), "results": results}

    # ------------------------------------------------------------------
    # 人工接管
    # ------------------------------------------------------------------

    def takeover(
        self,
        operator: str,
        action: str,
        mission_id: str | None = None,
        robot_id: str | None = None,
        note: str = "",
        details: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> TakeoverRecord:
        """登记一次人工接管；记录永久保留并随状态持久化。"""

        moment = (at or self._now()).isoformat()
        record = TakeoverRecord(
            takeover_id=f"to-{len(self.state.takeovers) + 1:04d}",
            at=moment,
            operator=operator,
            action=action,
            mission_id=mission_id,
            robot_id=robot_id,
            note=note,
            details=dict(details or {}),
        )
        self.state.takeovers.append(record)
        if mission_id is not None:
            mission = self.state.missions.get(mission_id)
            if mission is None:
                raise ContractError(f"未知任务 {mission_id}")
            if action == "resume" and mission.status == "suspended":
                mission.status = "pending"
                mission.history.append(
                    {"at": moment, "kind": "resumed", "by": operator, "note": note}
                )
            elif action == "cancel" and not mission.is_final:
                mission.status = "cancelled"
                mission.finalized_at = moment
                mission.history.append(
                    {"at": moment, "kind": "cancelled", "by": operator, "note": note}
                )
                robot = self.state.robots.get(mission.robot_id)
                if robot and mission_id in robot.queue:
                    robot.queue.remove(mission_id)
                if robot and robot.active_mission == mission_id:
                    robot.active_mission = None
                self.processor._release_mission_reservations(self.state, mission, moment, "takeover_cancel")
        self.processor._audit(
            self.state,
            "takeover",
            f"人工接管：{operator} 执行 {action}"
            + (f"（任务 {mission_id}）" if mission_id else "")
            + (f"（机器人 {robot_id}）" if robot_id else ""),
            data={
                "takeover_id": record.takeover_id,
                "operator": operator,
                "action": action,
                "mission_id": mission_id,
                "robot_id": robot_id,
                "note": note,
            },
            at=moment,
        )
        return record

    # ------------------------------------------------------------------
    # 值班视图
    # ------------------------------------------------------------------

    def next_actions(self, now: datetime | None = None) -> dict[str, Any]:
        return build_next_actions(self.engine, self.state, now or self._now())

    def blockers(self, now: datetime | None = None) -> dict[str, Any]:
        return build_blockers(self.engine, self.state, now or self._now())

    def risk(self) -> dict[str, Any]:
        return build_risk_report(self.state)

    def delivery_record(self, mission_id: str) -> dict[str, Any]:
        return build_delivery_record(self.state, mission_id)

    def _now(self) -> datetime:
        if self.state.last_clock:
            return parse_ts(self.state.last_clock)
        return datetime.now(timezone.utc)
