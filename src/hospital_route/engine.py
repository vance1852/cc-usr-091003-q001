"""调度引擎：合法路线搜索、时段预留与污染传播。

路线合法性统一由 ``_check_leg`` 判定，返回结构化阻断依据；
共享窄道与电梯的预留按区间重叠计数，任何时刻不得超过容量（不超卖）。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .model import EDGE_KIND_RANK, RobotProfile, RuleSet, Topology
from .state import (
    DoorRecord,
    EventProcessor,
    Mission,
    Reservation,
    RobotState,
    SystemState,
    parse_ts,
)

BLOCK_LABELS = {
    "zone_closed": "区域临时封控",
    "edge_closed": "通道临时封控",
    "zone_contaminated": "区域污染未消毒",
    "elevator_contaminated": "电梯轿厢污染未消毒",
    "no_valid_certificate": "消毒凭证缺失或过期",
    "door_pending": "门禁尚未回执",
    "door_denied": "门禁拒绝通行",
    "door_failed": "门禁故障",
    "elevator_payload": "电梯不承运该载荷",
    "elevator_access": "机器人无电梯权限",
    "robot_tier": "机器人污染等级不足",
    "temp_limit": "载荷温控时限不足",
    "robot_offline": "机器人离线",
    "robot_payload": "机器人不承运该载荷",
    "capacity": "共享资源时段已满",
    "no_route": "无可通行路线",
}

# 触发人工处置暂停的阻断代码；其余阻断（封控、容量、待回执）表现为等待
SUSPEND_CODES = frozenset(
    {"door_denied", "door_failed", "temp_limit", "no_valid_certificate", "elevator_contaminated", "zone_contaminated"}
)


@dataclass(frozen=True)
class BlockReason:
    code: str
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Leg:
    edge_id: str
    from_zone: str
    to_zone: str
    kind: str
    elevator_id: str | None
    base_cost: float
    direction: str | None


@dataclass
class Candidate:
    legs: tuple[Leg, ...]
    total_cost: float
    arrive: datetime
    windows: tuple[tuple[str, str, str, str | None], ...]  # (resource_id, kind, start_iso, direction)
    reasons: tuple[BlockReason, ...] = ()


@dataclass
class Legality:
    ok: bool
    reasons: list[BlockReason]


class Engine:
    def __init__(
        self,
        topology: Topology,
        rules: RuleSet,
        robots: Mapping[str, RobotProfile],
        processor: EventProcessor,
    ) -> None:
        self.topology = topology
        self.rules = rules
        self.robots = robots
        self.processor = processor

    # ------------------------------------------------------------------
    # 资源占用查询
    # ------------------------------------------------------------------

    def active_closures(self, state: SystemState) -> tuple[set[str], set[str]]:
        zones: set[str] = set()
        edges: set[str] = set()
        for closure in state.closures.values():
            if closure.status != "active":
                continue
            if closure.scope == "zone":
                zones.add(closure.target)
            else:
                edges.add(closure.target)
        return zones, edges

    def valid_certificate(
        self, state: SystemState, target: str, at: datetime
    ) -> tuple[bool, dict[str, Any]]:
        """目标在 at 时刻是否持有有效消毒凭证；返回证据。"""

        best: tuple[str, str] | None = None  # (certificate_id, valid_until)
        for cert in state.certificates.values():
            if cert.status != "confirmed" or cert.target != target:
                continue
            valid_from = parse_ts(cert.valid_from)
            valid_until = parse_ts(cert.valid_until)
            if valid_from <= at < valid_until:
                if best is None or cert.valid_until > best[1]:
                    best = (cert.certificate_id, cert.valid_until)
        if best is None:
            return False, {"target": target, "checked_at": at.isoformat()}
        return True, {
            "target": target,
            "certificate_id": best[0],
            "valid_until": best[1],
            "checked_at": at.isoformat(),
        }

    def _window_overlaps(self, a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
        return a_start < b_end and b_start < a_end

    def capacity_available(
        self,
        state: SystemState,
        resource_id: str,
        direction: str | None,
        start: datetime,
        end: datetime,
        capacity: int,
        ignore_mission: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """检查 [start, end) 时段内资源是否超容量；返回冲突证据。"""

        conflicts: list[dict[str, Any]] = []
        for reservation in state.reservations.values():
            if reservation.status != "active" or reservation.resource_id != resource_id:
                continue
            if ignore_mission is not None and reservation.mission_id == ignore_mission:
                continue
            if direction is not None and reservation.direction is not None and reservation.direction != direction:
                continue
            r_start = parse_ts(reservation.start)
            r_end = parse_ts(reservation.end)
            if self._window_overlaps(start, end, r_start, r_end):
                conflicts.append(
                    {
                        "reservation_id": reservation.reservation_id,
                        "mission_id": reservation.mission_id,
                        "start": reservation.start,
                        "end": reservation.end,
                    }
                )
        return len(conflicts) < capacity, conflicts

    # ------------------------------------------------------------------
    # 单条边的合法性
    # ------------------------------------------------------------------

    def _check_leg(
        self,
        state: SystemState,
        mission: Mission,
        robot: RobotState,
        profile: RobotProfile,
        edge_id: str,
        from_zone: str,
        to_zone: str,
        depart: datetime,
        arrive: datetime,
        closed_zones: set[str],
        closed_edges: set[str],
        ignore_mission: str | None,
    ) -> Legality:
        reasons: list[BlockReason] = []
        edge = self.topology.edge(edge_id)
        tier = self.rules.tier_of(mission.payload_class)

        if edge_id in closed_edges:
            closure = self._closure_of(state, "edge", edge_id)
            reasons.append(
                BlockReason(
                    "edge_closed",
                    f"通道 {edge_id} 处于临时封控",
                    {"edge_id": edge_id, "closure": closure},
                )
            )
        for zone_id, label in ((from_zone, "起点"), (to_zone, "讫点")):
            if zone_id in closed_zones:
                closure = self._closure_of(state, "zone", zone_id)
                reasons.append(
                    BlockReason(
                        "zone_closed",
                        f"{label}区域 {zone_id} 处于临时封控",
                        {"zone_id": zone_id, "closure": closure},
                    )
                )
            zone_level = state.contamination_of_zone(zone_id)
            if self.rules.contamination_tiers[zone_level] > tier:
                ok, cert_evidence = self.valid_certificate(state, zone_id, depart)
                if not ok:
                    reasons.append(
                        BlockReason(
                            "zone_contaminated",
                            f"{label}区域 {zone_id} 污染档位 {zone_level} 超出载荷耐受，"
                            f"且无有效消毒凭证",
                            {"zone_id": zone_id, "level": zone_level, "certificate": cert_evidence},
                        )
                    )

        if edge.door_required:
            door = self._door_of(state, edge_id)
            if door is None:
                reasons.append(
                    BlockReason(
                        "door_pending",
                        f"通道 {edge_id} 的门禁尚未回执",
                        {"edge_id": edge_id, "door_id": edge.door_id},
                    )
                )
            elif door.result == "denied":
                reasons.append(
                    BlockReason(
                        "door_denied",
                        f"门禁 {door.door_id} 拒绝通行",
                        {
                            "door_id": door.door_id,
                            "event_id": door.event_id,
                            "occurred_at": door.occurred_at,
                        },
                    )
                )
            elif door.result == "failed":
                reasons.append(
                    BlockReason(
                        "door_failed",
                        f"门禁 {door.door_id} 故障未放行",
                        {
                            "door_id": door.door_id,
                            "event_id": door.event_id,
                            "occurred_at": door.occurred_at,
                        },
                    )
                )

        if edge.kind == "elevator":
            elevator_id = edge.elevator_id
            assert elevator_id is not None
            elevator = self.topology.elevators[elevator_id]
            if mission.payload_class not in elevator.allowed_payloads:
                reasons.append(
                    BlockReason(
                        "elevator_payload",
                        f"电梯 {elevator_id} 不承运载荷 {mission.payload_class}",
                        {"elevator_id": elevator_id, "allowed": list(elevator.allowed_payloads)},
                    )
                )
            if elevator_id not in profile.elevator_access:
                reasons.append(
                    BlockReason(
                        "elevator_access",
                        f"机器人 {profile.robot_id} 无电梯 {elevator_id} 使用权限",
                        {"elevator_id": elevator_id, "robot_id": profile.robot_id},
                    )
                )
            car_level = state.contamination_of_elevator(elevator_id)
            if self.rules.contamination_tiers[car_level] > tier:
                ok, cert_evidence = self.valid_certificate(state, elevator_id, depart)
                if not ok:
                    reasons.append(
                        BlockReason(
                            "elevator_contaminated",
                            f"电梯 {elevator_id} 轿厢污染档位 {car_level} 超出载荷耐受，"
                            f"且无有效消毒凭证",
                            {"elevator_id": elevator_id, "level": car_level, "certificate": cert_evidence},
                        )
                    )
            ok, conflicts = self.capacity_available(
                state, edge_id, None, depart, arrive, elevator.capacity, ignore_mission
            )
            if not ok:
                reasons.append(
                    BlockReason(
                        "capacity",
                        f"电梯 {elevator_id} 在 {depart.isoformat()}–{arrive.isoformat()} 时段已满",
                        {"elevator_id": elevator_id, "conflicts": conflicts},
                    )
                )
        elif edge.kind == "narrow":
            direction = "forward" if from_zone == edge.a else "reverse"
            ok, conflicts = self.capacity_available(
                state, edge_id, direction, depart, arrive, edge.capacity, ignore_mission
            )
            if not ok:
                reasons.append(
                    BlockReason(
                        "capacity",
                        f"窄道 {edge_id} 同向时段 {depart.isoformat()}–{arrive.isoformat()} 已满",
                        {"edge_id": edge_id, "direction": direction, "conflicts": conflicts},
                    )
                )
        return Legality(ok=not reasons, reasons=reasons)

    def _closure_of(self, state: SystemState, scope: str, target: str) -> dict[str, Any]:
        for closure in state.closures.values():
            if closure.status == "active" and closure.scope == scope and closure.target == target:
                return {
                    "closure_id": closure.closure_id,
                    "reason": closure.reason,
                    "closed_at": closure.closed_at,
                }
        return {}

    def _door_of(self, state: SystemState, edge_id: str) -> DoorRecord | None:
        """该边上最近一次门禁回执（按发生时刻，平手按请求号保证确定性）。"""

        best: DoorRecord | None = None
        for record in state.doors.values():
            if record.edge_id != edge_id or record.result is None:
                continue
            if best is None or (record.occurred_at or "", record.request_id) >= (
                best.occurred_at or "",
                best.request_id,
            ):
                best = record
        return best

    # ------------------------------------------------------------------
    # 路线搜索（分层展开，保证确定性）
    # ------------------------------------------------------------------

    def _expand_groups(
        self,
        state: SystemState,
        mission: Mission,
        robot: RobotState,
        profile: RobotProfile,
        zone_id: str,
        depart: datetime,
        closed_zones: set[str],
        closed_edges: set[str],
        ignore_mission: str | None,
    ) -> list[tuple[int, list[tuple[Leg, Legality, datetime]]]]:
        """返回 [(层级, [(边, 合法性, 到达时刻)])]，按层级与 edge_id 排序。"""

        zone = self.topology.zone(zone_id)
        layered: dict[int, list[tuple[str, Leg, Legality, datetime]]] = {}
        for group_index, group in enumerate(zone.adjacency):
            for edge_id in group:
                edge = self.topology.edge(edge_id)
                to_zone = edge.other(zone_id)
                if edge.kind == "elevator":
                    elevator = self.topology.elevators[edge.elevator_id]  # type: ignore[index]
                    cost = elevator.cycle_cost
                else:
                    cost = edge.base_cost
                arrive = depart + timedelta(seconds=cost)
                leg = Leg(
                    edge_id=edge_id,
                    from_zone=zone_id,
                    to_zone=to_zone,
                    kind=edge.kind,
                    elevator_id=edge.elevator_id,
                    base_cost=cost,
                    direction=("forward" if zone_id == edge.a else "reverse") if edge.kind == "narrow" else None,
                )
                legality = self._check_leg(
                    state, mission, robot, profile, edge_id, zone_id, to_zone,
                    depart, arrive, closed_zones, closed_edges, ignore_mission,
                )
                layered.setdefault(EDGE_KIND_RANK[edge.kind], []).append((edge_id, leg, legality, arrive))
        result: list[tuple[int, list[tuple[Leg, Legality, datetime]]]] = []
        for rank in sorted(layered):
            entries = sorted(layered[rank], key=lambda item: item[0])
            result.append((rank, [(leg, legality, arrive) for _, leg, legality, arrive in entries]))
        return result

    def find_route(
        self,
        state: SystemState,
        mission: Mission,
        start: datetime,
        ignore_mission: str | None = None,
        collect_blocked: bool = True,
    ) -> tuple[Candidate | None, list[BlockReason]]:
        """为任务搜索一条合法路线；失败时返回阻断依据。"""

        robot = state.robots[mission.robot_id]
        profile = self.robots[mission.robot_id]
        closed_zones, closed_edges = self.active_closures(state)
        origin = mission.origin
        destination = mission.destination

        preflight = self._preflight(state, mission, robot, profile, start)
        if preflight:
            return None, preflight

        # Dijkstra：状态为区域，代价为累计秒数；同代价按 edge_id 序列保证确定性
        best: dict[str, float] = {origin: 0.0}
        # (cost, tiebreak_path, zone, path)
        counter = 0
        heap: list[tuple[float, tuple[str, ...], str, tuple[Leg, ...]]] = [
            (0.0, (), origin, ())
        ]
        blocked: dict[tuple[str, str], BlockReason] = {}
        best_candidate: Candidate | None = None
        best_key: tuple[float, tuple[str, ...]] | None = None

        while heap:
            cost, tie, zone_id, path = heapq.heappop(heap)
            if best_key is not None and (cost, tie) >= best_key:
                continue
            if zone_id == destination:
                candidate = self._build_candidate(state, mission, path, start)
                key = (candidate.total_cost, tuple(leg.edge_id for leg in path))
                if best_key is None or key < best_key:
                    best_key = key
                    best_candidate = candidate
                continue
            if cost > best.get(zone_id, float("inf")):
                continue
            depart = start + timedelta(seconds=cost)
            for _rank, entries in self._expand_groups(
                state, mission, robot, profile, zone_id, depart,
                closed_zones, closed_edges, ignore_mission,
            ):
                for leg, legality, _arrive in entries:
                    if not legality.ok:
                        if collect_blocked:
                            for reason in legality.reasons:
                                blocked.setdefault((leg.edge_id, reason.code), reason)
                        continue
                    new_cost = cost + leg.base_cost
                    new_tie = tuple(list(tie) + [leg.edge_id])
                    if new_cost < best.get(leg.to_zone, float("inf")):
                        best[leg.to_zone] = new_cost
                        heapq.heappush(heap, (new_cost, new_tie, leg.to_zone, path + (leg,)))
        if best_candidate is not None:
            temp_reason = self._temp_deadline_reason(mission, best_candidate.arrive)
            if temp_reason is not None:
                return None, [temp_reason]
            return best_candidate, []
        reasons = list(blocked.values())
        if not reasons:
            reasons = [
                BlockReason(
                    "no_route",
                    f"从 {origin} 到 {destination} 不存在连通路线",
                    {"origin": origin, "destination": destination},
                )
            ]
        return None, reasons

    def _temp_deadline_reason(self, mission: Mission, arrive: datetime) -> BlockReason | None:
        limit = self.rules.temp_limits.get(mission.payload_class)
        if limit is None:
            return None
        deadline = parse_ts(mission.created_at) + timedelta(seconds=limit)
        if arrive > deadline:
            return BlockReason(
                "temp_limit",
                f"最快路线预计 {arrive.isoformat()} 送达，超过温控时限 {deadline.isoformat()}",
                {
                    "limit_seconds": limit,
                    "created_at": mission.created_at,
                    "deadline": deadline.isoformat(),
                    "eta": arrive.isoformat(),
                },
            )
        return None

    def _preflight(
        self, state: SystemState, mission: Mission, robot: RobotState, profile: RobotProfile, start: datetime
    ) -> list[BlockReason]:
        reasons: list[BlockReason] = []
        if not robot.online:
            reasons.append(
                BlockReason(
                    "robot_offline",
                    f"机器人 {robot.robot_id} 离线，等待恢复或人工接管",
                    {"robot_id": robot.robot_id},
                )
            )
        if mission.payload_class not in profile.payload_classes:
            reasons.append(
                BlockReason(
                    "robot_payload",
                    f"机器人 {profile.robot_id} 不承运载荷 {mission.payload_class}",
                    {"robot_id": profile.robot_id, "payload_class": mission.payload_class},
                )
            )
        tier = self.rules.tier_of(mission.payload_class)
        if tier > profile.max_tier:
            reasons.append(
                BlockReason(
                    "robot_tier",
                    f"载荷污染等级 {tier} 超出机器人 {profile.robot_id} 上限 {profile.max_tier}",
                    {"robot_id": profile.robot_id, "payload_tier": tier, "max_tier": profile.max_tier},
                )
            )
        limit = self.rules.temp_limits.get(mission.payload_class)
        if limit is not None:
            deadline = parse_ts(mission.created_at) + timedelta(seconds=limit)
            if start >= deadline:
                reasons.append(
                    BlockReason(
                        "temp_limit",
                        f"载荷温控时限已于 {deadline.isoformat()} 到期",
                        {
                            "limit_seconds": limit,
                            "created_at": mission.created_at,
                            "deadline": deadline.isoformat(),
                        },
                    )
                )
        return reasons

    def _build_candidate(
        self, state: SystemState, mission: Mission, path: tuple[Leg, ...], start: datetime
    ) -> Candidate:
        windows: list[tuple[str, str, str, str | None]] = []
        cursor = start
        for leg in path:
            arrive = cursor + timedelta(seconds=leg.base_cost)
            if leg.kind in ("narrow", "elevator"):
                windows.append((leg.edge_id, leg.kind, cursor.isoformat(), leg.direction))
            cursor = arrive
        total = (cursor - start).total_seconds()
        return Candidate(legs=path, total_cost=total, arrive=cursor, windows=tuple(windows))

    # ------------------------------------------------------------------
    # 计划提交：预留 + 污染传播
    # ------------------------------------------------------------------

    def commit_plan(
        self,
        state: SystemState,
        mission: Mission,
        candidate: Candidate,
        start: datetime,
        evidence: Mapping[str, Any],
    ) -> list[Reservation]:
        """为任务登记时段预留并传播通行污染；调用前必须已通过 find_route。"""

        # 重新规划时先作废旧预留，避免残留的时段占用超卖容量
        for reservation in state.reservations.values():
            if reservation.mission_id == mission.mission_id and reservation.status == "active":
                reservation.status = "released"
        created: list[Reservation] = []
        for index, (resource_id, kind, start_iso, direction) in enumerate(candidate.windows):
            start_dt = parse_ts(start_iso)
            leg = next(leg for leg in candidate.legs if leg.edge_id == resource_id)
            end_dt = start_dt + timedelta(seconds=leg.base_cost)
            reservation = Reservation(
                reservation_id=f"rsv-{mission.mission_id}-{index + 1}",
                mission_id=mission.mission_id,
                resource_id=resource_id,
                resource_kind=kind,
                direction=direction,
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                evidence=dict(evidence),
            )
            state.reservations[reservation.reservation_id] = reservation
            created.append(reservation)
            self.processor._audit(
                state,
                "reservation_committed",
                f"任务 {mission.mission_id} 预留 {kind} {resource_id}"
                f"（{reservation.start}–{reservation.end}）",
                data={
                    "reservation_id": reservation.reservation_id,
                    "mission_id": mission.mission_id,
                    "resource_id": resource_id,
                    "start": reservation.start,
                    "end": reservation.end,
                },
            )
        self._propagate_transit_contamination(state, mission, candidate, start, evidence)
        mission.status = "en_route"
        mission.evidence["planned_route"] = {
            "legs": [
                {
                    "edge_id": leg.edge_id,
                    "from": leg.from_zone,
                    "to": leg.to_zone,
                    "kind": leg.kind,
                    "elevator_id": leg.elevator_id,
                }
                for leg in candidate.legs
            ],
            "planned_at": start.isoformat(),
            "arrive_estimate": candidate.arrive.isoformat(),
        }
        mission.history.append(
            {
                "at": start.isoformat(),
                "kind": "route_committed",
                "legs": [leg.edge_id for leg in candidate.legs],
            }
        )
        robot = state.robots[mission.robot_id]
        robot.active_mission = mission.mission_id
        return created

    def _propagate_transit_contamination(
        self,
        state: SystemState,
        mission: Mission,
        candidate: Candidate,
        start: datetime,
        evidence: Mapping[str, Any],
    ) -> None:
        """污染载荷通行后，其携带污染传播到途经区域与电梯。

        密封载荷（药品、标本箱）不污染途经环境；只有敞开污染物
        （污染织物、废弃物，档位 >= 2）在通行时留下污染。
        """

        tier = self.rules.tier_of(mission.payload_class)
        if tier < 2:
            return
        level = self._level_for_tier(tier)
        if level is None:
            return
        base_evidence = {
            "mission_id": mission.mission_id,
            "payload_class": mission.payload_class,
            "route": [leg.edge_id for leg in candidate.legs],
            **dict(evidence),
        }
        at = start.isoformat()
        for leg in candidate.legs:
            if leg.kind == "elevator" and leg.elevator_id is not None:
                self.processor.contaminate_elevator(
                    state, leg.elevator_id, level, at, "transit", base_evidence
                )
            else:
                self.processor.contaminate_zone(
                    state, leg.to_zone, level, at, "transit", base_evidence
                )

    def _level_for_tier(self, tier: int) -> str | None:
        for level, value in self.rules.contamination_tiers.items():
            if value == tier:
                return level
        return None

    # ------------------------------------------------------------------
    # 下一项合法动作
    # ------------------------------------------------------------------

    def next_actions(self, state: SystemState, now: datetime) -> list[dict[str, Any]]:
        """对每台机器人给出下一项合法动作或等待依据。"""

        actions: list[dict[str, Any]] = []
        for robot_id in sorted(state.robots):
            robot = state.robots[robot_id]
            profile = self.robots.get(robot_id)
            entry: dict[str, Any] = {
                "robot_id": robot_id,
                "zone": robot.zone,
                "online": robot.online,
                "contamination": robot.contamination,
            }
            if profile is None:
                entry.update(
                    {
                        "action": "blocked",
                        "reasons": [
                            {
                                "code": "unknown_robot",
                                "message": f"机器人 {robot_id} 无档案，禁止调度",
                                "evidence": {},
                            }
                        ],
                    }
                )
                actions.append(entry)
                continue
            mission = self._next_mission(state, robot)
            if mission is None:
                entry.update({"action": "idle", "reasons": []})
                actions.append(entry)
                continue
            entry["mission_id"] = mission.mission_id
            if mission.status == "suspended":
                entry.update(
                    {
                        "action": "await_manual",
                        "reasons": [
                            {
                                "code": reason["code"],
                                "message": reason["message"],
                                "evidence": reason.get("evidence", {}),
                            }
                            for reason in mission.hold_reasons[-3:]
                        ],
                    }
                )
                actions.append(entry)
                continue
            candidate, reasons = self.find_route(state, mission, now, ignore_mission=mission.mission_id)
            if candidate is None:
                entry.update(
                    {
                        "action": "wait",
                        "reasons": [
                            {"code": r.code, "message": r.message, "evidence": r.evidence} for r in reasons
                        ],
                    }
                )
                actions.append(entry)
                continue
            first = candidate.legs[0]
            entry.update(
                {
                    "action": "dispatch",
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
                    "next_leg": {
                        "edge_id": first.edge_id,
                        "to": first.to_zone,
                        "kind": first.kind,
                        "elevator_id": first.elevator_id,
                    },
                    "eta": candidate.arrive.isoformat(),
                    "reasons": [],
                }
            )
            actions.append(entry)
        return actions

    def _next_mission(self, state: SystemState, robot: RobotState) -> Mission | None:
        for mission_id in robot.queue:
            mission = state.missions.get(mission_id)
            if mission is not None and not mission.is_final:
                return mission
        return None
