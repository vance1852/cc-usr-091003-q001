"""运行时状态与事件处理。

事件按 (received_at, sequence, event_id) 的全序重放，保证乱序上报得到确定性结果；
机器人离线补传的事件（received_at 晚于已签认事实）不得倒改这些事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from .contracts import ContractError, EventEnvelope
from .model import RuleSet, Topology

# 已签认事实：离线补传不得倒改
FINAL_MISSION_STATUSES = ("delivered", "cancelled")
FINAL_CLOSURE_STATUSES = ("lifted",)
FINAL_CLEANING_STATUSES = ("confirmed",)
# 门禁回执终态：同一门禁重送的回执不得再次推进
FINAL_DOOR_RESULTS = ("granted", "denied", "failed")


def parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError("时间必须包含时区")
    return parsed


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str
    event_id: str


@dataclass
class DoorRecord:
    door_id: str
    edge_id: str
    request_id: str
    result: str | None = None
    event_id: str | None = None
    occurred_at: str | None = None


@dataclass
class Mission:
    mission_id: str
    robot_id: str
    payload_class: str
    origin: str
    destination: str
    status: str = "pending"
    created_at: str = ""
    hold_reasons: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    finalized_at: str | None = None

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_MISSION_STATUSES


@dataclass
class Reservation:
    reservation_id: str
    mission_id: str
    resource_id: str
    resource_kind: str
    direction: str | None
    start: str
    end: str
    status: str = "active"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Closure:
    closure_id: str
    target: str
    scope: str
    reason: str
    status: str = "active"
    closed_at: str = ""
    lifted_at: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CleaningCert:
    certificate_id: str
    target: str
    valid_from: str
    valid_until: str
    status: str = "confirmed"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RobotState:
    robot_id: str
    zone: str
    online: bool = True
    contamination: str = "clean"
    elevator_contamination: dict[str, str] = field(default_factory=dict)
    queue: list[str] = field(default_factory=list)
    active_mission: str | None = None
    updated_at: str = ""


@dataclass
class TakeoverRecord:
    takeover_id: str
    at: str
    operator: str
    action: str
    mission_id: str | None
    robot_id: str | None
    note: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEntry:
    seq: int
    at: str
    kind: str
    event_id: str | None
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemState:
    scenario: str
    rule_version: str
    missions: dict[str, Mission] = field(default_factory=dict)
    reservations: dict[str, Reservation] = field(default_factory=dict)
    closures: dict[str, Closure] = field(default_factory=dict)
    certificates: dict[str, CleaningCert] = field(default_factory=dict)
    doors: dict[str, DoorRecord] = field(default_factory=dict)
    robots: dict[str, RobotState] = field(default_factory=dict)
    zone_contamination: dict[str, str] = field(default_factory=dict)
    elevator_contamination: dict[str, str] = field(default_factory=dict)
    takeovers: list[TakeoverRecord] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    processed_events: set[str] = field(default_factory=set)
    rejections: list[Rejection] = field(default_factory=list)
    last_clock: str = ""

    def contamination_of_zone(self, zone_id: str) -> str:
        return self.zone_contamination.get(zone_id, "clean")

    def contamination_of_elevator(self, elevator_id: str) -> str:
        return self.elevator_contamination.get(elevator_id, "clean")


# ---------------------------------------------------------------------------
# 事件排序与属性读取
# ---------------------------------------------------------------------------


def _sequence(event: EventEnvelope) -> int:
    value = event.attributes.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def event_order_key(event: EventEnvelope) -> tuple[datetime, int, str]:
    return (parse_ts(event.received_at), _sequence(event), event.event_id)


def order_events(events: Iterable[EventEnvelope]) -> list[EventEnvelope]:
    return sorted(events, key=event_order_key)


def _attr(event: EventEnvelope, key: str) -> Any:
    if key not in event.attributes:
        raise ContractError(f"事件 {event.event_id} 缺少属性 {key}")
    return event.attributes[key]


def _attr_str(event: EventEnvelope, key: str) -> str:
    value = _attr(event, key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"事件 {event.event_id} 属性 {key} 必须是非空字符串")
    return value


def _attr_optional_str(event: EventEnvelope, key: str) -> str | None:
    value = event.attributes.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"事件 {event.event_id} 属性 {key} 必须是字符串")
    return value


def _attr_int(event: EventEnvelope, key: str) -> int:
    value = _attr(event, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"事件 {event.event_id} 属性 {key} 必须是整数")
    return value


def _attr_str_list(event: EventEnvelope, key: str) -> list[str]:
    value = _attr(event, key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"事件 {event.event_id} 属性 {key} 必须是字符串数组")
    return list(value)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


class EventProcessor:
    """把现场事件应用到系统状态；静态资料只读注入。"""

    def __init__(self, topology: Topology, rules: RuleSet) -> None:
        self.topology = topology
        self.rules = rules

    # -- 工具 ----------------------------------------------------------------

    def _audit(
        self,
        state: SystemState,
        kind: str,
        summary: str,
        event_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        at: str | None = None,
    ) -> None:
        state.audit.append(
            AuditEntry(
                seq=len(state.audit) + 1,
                at=at or state.last_clock or "",
                kind=kind,
                event_id=event_id,
                summary=summary,
                data=dict(data or {}),
            )
        )

    def _clock(self, state: SystemState, event: EventEnvelope) -> None:
        if event.received_at > state.last_clock:
            state.last_clock = event.received_at

    def _reject(self, state: SystemState, event: EventEnvelope, code: str, message: str) -> None:
        state.rejections.append(Rejection(code=code, message=message, event_id=event.event_id))
        self._audit(state, "event_rejected", message, event.event_id, {"code": code})

    def _require_robot(self, state: SystemState, robot_id: str) -> RobotState | None:
        return state.robots.get(robot_id)

    # -- 污染传播 --------------------------------------------------------------

    def _spread_contamination(
        self,
        state: SystemState,
        source_kind: str,
        source_id: str,
        new_level: str,
        at: str,
        cause: str,
        evidence: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """把污染档位传播到关联区域/电梯，返回传播记录（含证据）。"""

        propagated: list[dict[str, Any]] = []
        for target_level in self.rules.contamination_spread.get(new_level, ()):
            if source_kind == "zone":
                zone = self.topology.zone(source_id)
                for group in zone.adjacency:
                    for edge_id in group:
                        edge = self.topology.edge(edge_id)
                        neighbor_id = edge.other(source_id)
                        if edge.kind == "elevator":
                            elevator_id = edge.elevator_id
                            assert elevator_id is not None
                            current = state.contamination_of_elevator(elevator_id)
                            if self.rules.contamination_tiers[current] < self.rules.contamination_tiers[target_level]:
                                state.elevator_contamination[elevator_id] = target_level
                                propagated.append(
                                    {
                                        "target_kind": "elevator",
                                        "target_id": elevator_id,
                                        "level": target_level,
                                        "via_edge": edge_id,
                                    }
                                )
                        else:
                            current = state.contamination_of_zone(neighbor_id)
                            if self.rules.contamination_tiers[current] < self.rules.contamination_tiers[target_level]:
                                state.zone_contamination[neighbor_id] = target_level
                                propagated.append(
                                    {
                                        "target_kind": "zone",
                                        "target_id": neighbor_id,
                                        "level": target_level,
                                        "via_edge": edge_id,
                                    }
                                )
            elif source_kind == "elevator":
                elevator = self.topology.elevators[source_id]
                for zone_id in elevator.serves:
                    current = state.contamination_of_zone(zone_id)
                    if self.rules.contamination_tiers[current] < self.rules.contamination_tiers[target_level]:
                        state.zone_contamination[zone_id] = target_level
                        propagated.append(
                            {
                                "target_kind": "zone",
                                "target_id": zone_id,
                                "level": target_level,
                                "via_edge": elevator.edge_id,
                            }
                        )
            else:  # pragma: no cover - 防御
                raise ContractError(f"未知污染源类型 {source_kind}")
        if propagated:
            self._audit(
                state,
                "contamination_propagated",
                f"{source_kind} {source_id} 的污染外溢到 {len(propagated)} 个关联目标",
                data={
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "level": new_level,
                    "targets": propagated,
                    "cause": cause,
                    "evidence": dict(evidence),
                },
                at=at,
            )
        return propagated

    def contaminate_zone(
        self,
        state: SystemState,
        zone_id: str,
        level: str,
        at: str,
        cause: str,
        evidence: Mapping[str, Any],
        event_id: str | None = None,
    ) -> None:
        if level not in self.rules.contamination_tiers:
            raise ContractError(f"未知污染档位 {level}")
        current = state.contamination_of_zone(zone_id)
        if self.rules.contamination_tiers[level] <= self.rules.contamination_tiers[current]:
            return
        state.zone_contamination[zone_id] = level
        self._audit(
            state,
            "zone_contaminated",
            f"区域 {zone_id} 污染档位升为 {level}",
            event_id,
            {"zone": zone_id, "level": level, "cause": cause, "evidence": dict(evidence)},
            at=at,
        )
        self._spread_contamination(state, "zone", zone_id, level, at, cause, evidence)

    def contaminate_elevator(
        self,
        state: SystemState,
        elevator_id: str,
        level: str,
        at: str,
        cause: str,
        evidence: Mapping[str, Any],
        event_id: str | None = None,
    ) -> None:
        if level not in self.rules.contamination_tiers:
            raise ContractError(f"未知污染档位 {level}")
        current = state.contamination_of_elevator(elevator_id)
        if self.rules.contamination_tiers[level] <= self.rules.contamination_tiers[current]:
            return
        state.elevator_contamination[elevator_id] = level
        self._audit(
            state,
            "elevator_contaminated",
            f"电梯 {elevator_id} 污染档位升为 {level}",
            event_id,
            {"elevator": elevator_id, "level": level, "cause": cause, "evidence": dict(evidence)},
            at=at,
        )
        self._spread_contamination(state, "elevator", elevator_id, level, at, cause, evidence)

    # -- 任务 ------------------------------------------------------------------

    def _mission_hold(
        self,
        state: SystemState,
        mission: Mission,
        reason_code: str,
        message: str,
        at: str,
        evidence: Mapping[str, Any],
        event_id: str | None = None,
    ) -> None:
        if mission.is_final:
            return
        if mission.status != "suspended":
            mission.status = "suspended"
        mission.hold_reasons.append(
            {"code": reason_code, "message": message, "at": at, "evidence": dict(evidence)}
        )
        mission.history.append(
            {
                "at": at,
                "kind": "suspended",
                "reason": reason_code,
                "message": message,
                "evidence": dict(evidence),
            }
        )
        self._audit(
            state,
            "mission_suspended",
            f"任务 {mission.mission_id} 进入人工处置暂停：{message}",
            event_id,
            {"mission_id": mission.mission_id, "reason": reason_code, "evidence": dict(evidence)},
            at=at,
        )

    def _handle_mission_created(self, state: SystemState, event: EventEnvelope) -> None:
        mission_id = _attr_str(event, "mission_id")
        robot_id = _attr_str(event, "robot_id")
        payload_class = _attr_str(event, "payload_class")
        origin = _attr_str(event, "origin")
        destination = _attr_str(event, "destination")
        if mission_id in state.missions:
            self._reject(state, event, "duplicate_mission", f"任务 {mission_id} 已存在")
            return
        if payload_class not in self.rules.payload_classes:
            self._reject(state, event, "unknown_payload", f"未知载荷类别 {payload_class}")
            return
        try:
            self.topology.zone(origin)
            self.topology.zone(destination)
        except ContractError as exc:
            self._reject(state, event, "unknown_zone", str(exc))
            return
        robot = self._require_robot(state, robot_id)
        if robot is None:
            self._reject(state, event, "unknown_robot", f"未知机器人 {robot_id}")
            return
        mission = Mission(
            mission_id=mission_id,
            robot_id=robot_id,
            payload_class=payload_class,
            origin=origin,
            destination=destination,
            status="pending",
            created_at=event.occurred_at,
            evidence={"created_by": event.event_id, "received_at": event.received_at},
        )
        mission.history.append(
            {"at": event.received_at, "kind": "created", "event_id": event.event_id}
        )
        state.missions[mission_id] = mission
        robot.queue.append(mission_id)
        robot.updated_at = event.received_at
        self._audit(
            state,
            "mission_created",
            f"任务 {mission_id} 登记到机器人 {robot_id}（{payload_class}：{origin} → {destination}）",
            event.event_id,
            {
                "mission_id": mission_id,
                "robot_id": robot_id,
                "payload_class": payload_class,
                "origin": origin,
                "destination": destination,
            },
        )

    def _handle_mission_cancelled(self, state: SystemState, event: EventEnvelope) -> None:
        mission_id = _attr_str(event, "mission_id")
        mission = state.missions.get(mission_id)
        if mission is None:
            self._reject(state, event, "unknown_mission", f"未知任务 {mission_id}")
            return
        if mission.is_final:
            # 已签认任务不得倒改
            self._audit(
                state,
                "late_event_ignored",
                f"任务 {mission_id} 已签认为 {mission.status}，忽略迟到的取消事件",
                event.event_id,
                {"mission_id": mission_id, "final_status": mission.status},
            )
            return
        mission.status = "cancelled"
        mission.finalized_at = event.received_at
        mission.history.append(
            {"at": event.received_at, "kind": "cancelled", "event_id": event.event_id}
        )
        robot = state.robots.get(mission.robot_id)
        if robot and mission_id in robot.queue:
            robot.queue.remove(mission_id)
        if robot and robot.active_mission == mission_id:
            robot.active_mission = None
        self._release_mission_reservations(state, mission, event.received_at, "mission_cancelled")
        self._audit(
            state,
            "mission_cancelled",
            f"任务 {mission_id} 已取消",
            event.event_id,
            {"mission_id": mission_id},
        )

    def _release_mission_reservations(
        self, state: SystemState, mission: Mission, at: str, cause: str
    ) -> None:
        for reservation in state.reservations.values():
            if reservation.mission_id == mission.mission_id and reservation.status == "active":
                reservation.status = "released"
                self._audit(
                    state,
                    "reservation_released",
                    f"预留 {reservation.reservation_id} 释放（{cause}）",
                    data={
                        "reservation_id": reservation.reservation_id,
                        "mission_id": mission.mission_id,
                        "cause": cause,
                    },
                    at=at,
                )

    # -- 机器人 -----------------------------------------------------------------

    def _handle_robot_arrived(self, state: SystemState, event: EventEnvelope) -> None:
        robot_id = _attr_str(event, "robot_id")
        zone_id = _attr_str(event, "zone")
        mission_id = _attr_optional_str(event, "mission_id")
        try:
            self.topology.zone(zone_id)
        except ContractError as exc:
            self._reject(state, event, "unknown_zone", str(exc))
            return
        robot = self._require_robot(state, robot_id)
        if robot is None:
            self._reject(state, event, "unknown_robot", f"未知机器人 {robot_id}")
            return
        if mission_id is not None:
            mission = state.missions.get(mission_id)
            if mission is None:
                self._reject(state, event, "unknown_mission", f"未知任务 {mission_id}")
                return
            if mission.is_final:
                # 离线补传的到达不得倒改已签认任务
                self._audit(
                    state,
                    "late_event_ignored",
                    f"任务 {mission_id} 已签认为 {mission.status}，忽略补传的到达事件",
                    event.event_id,
                    {"mission_id": mission_id, "final_status": mission.status},
                )
                return
            if mission.robot_id != robot_id:
                self._reject(state, event, "robot_mismatch", f"任务 {mission_id} 不属于机器人 {robot_id}")
                return
            arrived_at_destination = zone_id == mission.destination
            mission.history.append(
                {
                    "at": event.received_at,
                    "kind": "arrived",
                    "zone": zone_id,
                    "event_id": event.event_id,
                }
            )
            if mission.status == "suspended":
                # 暂停中的任务只能由人工接管处置，到达事件不推进状态
                self._audit(
                    state,
                    "arrival_while_suspended",
                    f"任务 {mission_id} 处于人工处置暂停，到达 {zone_id} 仅登记不推进",
                    event.event_id,
                    {"mission_id": mission_id, "zone": zone_id},
                )
            elif arrived_at_destination:
                self._finalize_delivery(state, mission, robot, event)
            else:
                mission.status = "en_route"
                self._audit(
                    state,
                    "robot_arrived",
                    f"机器人 {robot_id} 到达中途区域 {zone_id}（任务 {mission_id}）",
                    event.event_id,
                    {"robot_id": robot_id, "zone": zone_id, "mission_id": mission_id},
                )
        else:
            mission = None
            self._audit(
                state,
                "robot_arrived",
                f"机器人 {robot_id} 到达 {zone_id}",
                event.event_id,
                {"robot_id": robot_id, "zone": zone_id},
            )
        robot.zone = zone_id
        robot.updated_at = event.received_at

    def _finalize_delivery(
        self, state: SystemState, mission: Mission, robot: RobotState, event: EventEnvelope
    ) -> None:
        """签认送达：不可再被补传事件倒改。"""

        # 温控超时在签认点复核：超时不得签认，转入人工处置
        limit = self.rules.temp_limits.get(mission.payload_class)
        if limit is not None:
            elapsed = (parse_ts(event.occurred_at) - parse_ts(mission.created_at)).total_seconds()
            if elapsed > limit:
                evidence = {
                    "limit_seconds": limit,
                    "elapsed_seconds": int(elapsed),
                    "created_at": mission.created_at,
                    "arrived_at": event.occurred_at,
                    "arrival_event": event.event_id,
                }
                mission.evidence["temp_breach"] = evidence
                self._mission_hold(
                    state,
                    mission,
                    "temp_limit_exceeded",
                    f"载荷温控超时（{int(elapsed)} 秒 > 上限 {limit} 秒），禁止签认送达",
                    event.received_at,
                    evidence,
                    event.event_id,
                )
                robot.zone = mission.destination
                return
        mission.status = "delivered"
        mission.finalized_at = event.received_at
        mission.evidence["delivered_by"] = event.event_id
        mission.evidence["delivered_at"] = event.occurred_at
        mission.history.append(
            {"at": event.received_at, "kind": "delivered", "event_id": event.event_id}
        )
        if robot.active_mission == mission.mission_id:
            robot.active_mission = None
        if mission.mission_id in robot.queue:
            robot.queue.remove(mission.mission_id)
        self._release_mission_reservations(state, mission, event.received_at, "mission_delivered")
        self._audit(
            state,
            "mission_delivered",
            f"任务 {mission.mission_id} 已签认送达 {mission.destination}",
            event.event_id,
            {"mission_id": mission.mission_id, "robot_id": robot.robot_id},
        )

    def _handle_robot_offline(self, state: SystemState, event: EventEnvelope) -> None:
        robot_id = _attr_str(event, "robot_id")
        robot = self._require_robot(state, robot_id)
        if robot is None:
            self._reject(state, event, "unknown_robot", f"未知机器人 {robot_id}")
            return
        robot.online = False
        robot.updated_at = event.received_at
        self._audit(
            state,
            "robot_offline",
            f"机器人 {robot_id} 离线",
            event.event_id,
            {"robot_id": robot_id},
        )

    def _handle_robot_online(self, state: SystemState, event: EventEnvelope) -> None:
        robot_id = _attr_str(event, "robot_id")
        zone_id = _attr_optional_str(event, "zone")
        robot = self._require_robot(state, robot_id)
        if robot is None:
            self._reject(state, event, "unknown_robot", f"未知机器人 {robot_id}")
            return
        robot.online = True
        if zone_id is not None:
            try:
                self.topology.zone(zone_id)
            except ContractError as exc:
                self._reject(state, event, "unknown_zone", str(exc))
                return
            robot.zone = zone_id
        robot.updated_at = event.received_at
        self._audit(
            state,
            "robot_online",
            f"机器人 {robot_id} 恢复在线",
            event.event_id,
            {"robot_id": robot_id, "zone": robot.zone},
        )

    # -- 门禁 ------------------------------------------------------------------

    def _handle_door_result(self, state: SystemState, event: EventEnvelope) -> None:
        door_id = _attr_str(event, "door_id")
        result = _attr_str(event, "result")
        if result not in FINAL_DOOR_RESULTS:
            self._reject(state, event, "bad_door_result", f"非法门禁结果 {result}")
            return
        edge_id = _attr_optional_str(event, "edge_id")
        request_id = _attr_optional_str(event, "request_id")
        mission_id = _attr_optional_str(event, "mission_id")
        # 同一门禁请求的回执只生效一次；重送不得再次推进
        door_key = request_id or door_id
        record = state.doors.get(door_key)
        if record is None:
            if edge_id is None:
                self._reject(state, event, "door_missing_edge", f"门禁 {door_id} 首次回执缺少 edge_id")
                return
            try:
                self.topology.edge(edge_id)
            except ContractError as exc:
                self._reject(state, event, "unknown_edge", str(exc))
                return
            record = DoorRecord(door_id=door_id, edge_id=edge_id, request_id=door_key)
            state.doors[door_key] = record
        if record.result is not None:
            self._audit(
                state,
                "duplicate_door_result",
                f"门禁请求 {door_key} 已有回执 {record.result}（事件 {record.event_id}），忽略重送",
                event.event_id,
                {
                    "door_key": door_key,
                    "door_id": door_id,
                    "existing_result": record.result,
                    "existing_event": record.event_id,
                    "repeated_result": result,
                },
            )
            return
        record.result = result
        record.event_id = event.event_id
        record.occurred_at = event.occurred_at
        evidence = {
            "door_id": door_id,
            "edge_id": record.edge_id,
            "result": result,
            "event_id": event.event_id,
            "occurred_at": event.occurred_at,
        }
        if mission_id is not None:
            evidence["mission_id"] = mission_id
        if request_id is not None:
            evidence["request_id"] = request_id
        self._audit(
            state,
            "door_result",
            f"门禁 {door_id} 回执 {result}",
            event.event_id,
            evidence,
        )
        if result == "granted":
            return
        # 拒绝/故障：关联任务进入人工处置暂停，不得另行绕过
        missions: list[Mission] = []
        if mission_id is not None:
            mission = state.missions.get(mission_id)
            if mission is not None and not mission.is_final:
                missions.append(mission)
        else:
            for mission in state.missions.values():
                if mission.is_final:
                    continue
                for reservation in state.reservations.values():
                    if (
                        reservation.mission_id == mission.mission_id
                        and reservation.status == "active"
                        and reservation.resource_id == record.edge_id
                    ):
                        missions.append(mission)
                        break
        for mission in missions:
            mission.evidence.setdefault("door_results", []).append(evidence)
            self._mission_hold(
                state,
                mission,
                f"door_{result}",
                f"门禁 {door_id} 回执 {result}，等待人工处置",
                event.received_at,
                evidence,
                event.event_id,
            )

    # -- 封控 ------------------------------------------------------------------

    def _handle_zone_closed(self, state: SystemState, event: EventEnvelope) -> None:
        closure_id = _attr_str(event, "closure_id")
        target = _attr_str(event, "target")
        scope = event.attributes.get("scope", "zone")
        if scope not in ("zone", "edge"):
            self._reject(state, event, "bad_scope", f"非法封控范围 {scope}")
            return
        reason = _attr_optional_str(event, "reason") or "临时封控"
        try:
            if scope == "zone":
                self.topology.zone(target)
            else:
                self.topology.edge(target)
        except ContractError as exc:
            self._reject(state, event, "unknown_target", str(exc))
            return
        existing = state.closures.get(closure_id)
        if existing is not None:
            if existing.status == "active":
                self._audit(
                    state,
                    "duplicate_closure",
                    f"封控 {closure_id} 已生效，忽略重复登记",
                    event.event_id,
                    {"closure_id": closure_id},
                )
            else:
                self._audit(
                    state,
                    "late_event_ignored",
                    f"封控 {closure_id} 已解除签认，忽略补传的关闭事件",
                    event.event_id,
                    {"closure_id": closure_id},
                )
            return
        closure = Closure(
            closure_id=closure_id,
            target=target,
            scope=scope,
            reason=reason,
            closed_at=event.occurred_at,
            evidence={"event_id": event.event_id, "received_at": event.received_at},
        )
        state.closures[closure_id] = closure
        self._audit(
            state,
            "closure_registered",
            f"{scope} {target} 封控生效（{reason}）",
            event.event_id,
            {"closure_id": closure_id, "target": target, "scope": scope, "reason": reason},
        )

    def _handle_zone_reopened(self, state: SystemState, event: EventEnvelope) -> None:
        closure_id = _attr_str(event, "closure_id")
        closure = state.closures.get(closure_id)
        if closure is None:
            self._reject(state, event, "unknown_closure", f"未知封控 {closure_id}")
            return
        if closure.status in FINAL_CLOSURE_STATUSES:
            self._audit(
                state,
                "late_event_ignored",
                f"封控 {closure_id} 已解除签认，忽略重复解除",
                event.event_id,
                {"closure_id": closure_id},
            )
            return
        closure.status = "lifted"
        closure.lifted_at = event.occurred_at
        closure.evidence["lifted_by"] = event.event_id
        self._audit(
            state,
            "closure_lifted",
            f"封控 {closure_id}（{closure.scope} {closure.target}）已解除",
            event.event_id,
            {"closure_id": closure_id, "target": closure.target},
        )

    # -- 消毒 ------------------------------------------------------------------

    def _handle_cleaning_completed(self, state: SystemState, event: EventEnvelope) -> None:
        certificate_id = _attr_str(event, "certificate_id")
        target = _attr_str(event, "target")
        target_kind = event.attributes.get("target_kind", "zone")
        if target_kind not in ("zone", "elevator"):
            self._reject(state, event, "bad_target_kind", f"非法消毒对象类型 {target_kind}")
            return
        valid_from_raw = _attr_str(event, "valid_from")
        valid_until_raw = _attr_str(event, "valid_until")
        try:
            valid_from = parse_ts(valid_from_raw)
            valid_until = parse_ts(valid_until_raw)
        except ContractError as exc:
            self._reject(state, event, "bad_validity", str(exc))
            return
        if valid_until <= valid_from:
            self._reject(state, event, "bad_validity", "消毒凭证有效期必须为正")
            return
        try:
            if target_kind == "zone":
                self.topology.zone(target)
            else:
                if target not in self.topology.elevators:
                    raise ContractError(f"未知电梯 {target}")
        except ContractError as exc:
            self._reject(state, event, "unknown_target", str(exc))
            return
        existing = state.certificates.get(certificate_id)
        if existing is not None:
            if existing.status in FINAL_CLEANING_STATUSES:
                # 已签认的清洁事件不得被补传倒改
                self._audit(
                    state,
                    "late_event_ignored",
                    f"消毒凭证 {certificate_id} 已签认，忽略补传/重复事件",
                    event.event_id,
                    {"certificate_id": certificate_id},
                )
            return
        cert = CleaningCert(
            certificate_id=certificate_id,
            target=target,
            valid_from=valid_from_raw,
            valid_until=valid_until_raw,
            evidence={
                "event_id": event.event_id,
                "target_kind": target_kind,
                "received_at": event.received_at,
            },
        )
        state.certificates[certificate_id] = cert
        if target_kind == "zone":
            state.zone_contamination[target] = "clean"
        else:
            state.elevator_contamination[target] = "clean"
        self._audit(
            state,
            "cleaning_confirmed",
            f"{target_kind} {target} 消毒完成并签认（凭证 {certificate_id}，有效至 {valid_until_raw}）",
            event.event_id,
            {
                "certificate_id": certificate_id,
                "target": target,
                "target_kind": target_kind,
                "valid_from": valid_from_raw,
                "valid_until": valid_until_raw,
            },
        )

    # -- 污染上报 ----------------------------------------------------------------

    def _handle_contamination_reported(self, state: SystemState, event: EventEnvelope) -> None:
        target = _attr_str(event, "target")
        target_kind = event.attributes.get("target_kind", "zone")
        level = _attr_str(event, "level")
        if target_kind not in ("zone", "elevator"):
            self._reject(state, event, "bad_target_kind", f"非法污染对象类型 {target_kind}")
            return
        if level not in self.rules.contamination_tiers:
            self._reject(state, event, "bad_level", f"未知污染档位 {level}")
            return
        evidence = {"event_id": event.event_id, "occurred_at": event.occurred_at}
        try:
            if target_kind == "zone":
                self.topology.zone(target)
                self.contaminate_zone(
                    state, target, level, event.received_at, "reported", evidence, event.event_id
                )
            else:
                if target not in self.topology.elevators:
                    raise ContractError(f"未知电梯 {target}")
                self.contaminate_elevator(
                    state, target, level, event.received_at, "reported", evidence, event.event_id
                )
        except ContractError as exc:
            self._reject(state, event, "unknown_target", str(exc))

    # -- 入口 ------------------------------------------------------------------

    def apply(self, state: SystemState, event: EventEnvelope) -> None:
        if event.event_id in state.processed_events:
            self._audit(
                state,
                "duplicate_event",
                f"事件 {event.event_id} 已处理，忽略重复投递",
                event.event_id,
            )
            return
        state.processed_events.add(event.event_id)
        self._clock(state, event)
        handler = self._handlers().get(event.kind)
        if handler is None:
            self._reject(state, event, "unknown_kind", f"未知事件类型 {event.kind}")
            return
        try:
            handler(state, event)
        except ContractError as exc:
            self._reject(state, event, "invalid_event", str(exc))

    def _handlers(self) -> dict[str, Any]:
        return {
            "mission_created": self._handle_mission_created,
            "mission_cancelled": self._handle_mission_cancelled,
            "robot_arrived": self._handle_robot_arrived,
            "robot_offline": self._handle_robot_offline,
            "robot_online": self._handle_robot_online,
            "door_result": self._handle_door_result,
            "zone_closed": self._handle_zone_closed,
            "zone_reopened": self._handle_zone_reopened,
            "cleaning_completed": self._handle_cleaning_completed,
            "contamination_reported": self._handle_contamination_reported,
        }
