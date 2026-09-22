"""调度引擎：按接收顺序应用事件，推导任务、预留与污染状态。

设计约定：
- 事件一律按 (received_at, event_id) 顺序应用；occurred_at 只描述事实发生时间。
- 已签认（delivered）或已取消（cancelled）的任务不可被后续补传事件倒改，
  迟到的到达/清洁等事件只作为证据归档。
- 门禁回执按 receipt_id 幂等，同一回执重送不会推进第二次。
- 温控超时、门禁拒绝、消毒凭证过期只会让任务进入待人工处置的暂停态，
  引擎绝不自行绕过。
- 引擎不读取系统时钟；当前时间取已应用事件的最大 received_at。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import EventEnvelope
from .layout import Layout
from .rules import Rules

PENDING = "pending"
DISPATCHED = "dispatched"
PAUSED = "paused"
DELIVERED = "delivered"
CANCELLED = "cancelled"

ACTIVE_STATUSES = (PENDING, DISPATCHED, PAUSED)
FINAL_STATUSES = (DELIVERED, CANCELLED)

# 暂停原因编码
PAUSE_TEMP_TIMEOUT = "temp_timeout"
PAUSE_DOOR_DENIED = "door_denied"
PAUSE_CERT_EXPIRED = "cert_expired"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Reservation:
    reservation_id: str
    resource_id: str
    mission_id: str
    robot_id: str
    start: str
    end: str
    rules_version: str
    released: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "resource_id": self.resource_id,
            "mission_id": self.mission_id,
            "robot_id": self.robot_id,
            "start": self.start,
            "end": self.end,
            "rules_version": self.rules_version,
            "released": self.released,
        }


@dataclass
class SegmentState:
    """区域或通道的动态状态（封控/污染/清洁记录）。"""

    closed: bool = False
    closed_by: str | None = None
    contaminated_since: str | None = None
    contamination_source: dict[str, Any] | None = None
    cleanings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Mission:
    mission_id: str
    robot_id: str
    payload_class: str
    origin: str
    destination: str
    sequence: int
    created_at: str
    created_event: str
    temp_limit_minutes: int | None
    status: str = PENDING
    route: list[str] = field(default_factory=list)
    reservations: list[str] = field(default_factory=list)
    dispatched_at: str | None = None
    rules_version: str = ""
    pause_reasons: list[dict[str, Any]] = field(default_factory=list)
    door_events: list[dict[str, Any]] = field(default_factory=list)
    arrivals: list[dict[str, Any]] = field(default_factory=list)
    cleaning_evidence: list[dict[str, Any]] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    late_events: list[dict[str, Any]] = field(default_factory=list)
    delivered_at: str | None = None
    signed: dict[str, Any] | None = None

    def temp_deadline(self) -> str | None:
        if self.temp_limit_minutes is None:
            return None
        return _iso(_parse(self.created_at) + timedelta(minutes=self.temp_limit_minutes))

    def summary(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "robot_id": self.robot_id,
            "payload_class": self.payload_class,
            "origin": self.origin,
            "destination": self.destination,
            "sequence": self.sequence,
            "status": self.status,
            "created_at": self.created_at,
            "temp_deadline": self.temp_deadline(),
            "route": list(self.route),
            "reservations": list(self.reservations),
            "rules_version": self.rules_version,
            "delivered_at": self.delivered_at,
            "pause_reasons": list(self.pause_reasons),
            "overrides": list(self.overrides),
        }


class DispatchEngine:
    """事件溯源的调度引擎；状态完全由已应用事件推导。"""

    def __init__(self, layout: Layout, rules: Rules) -> None:
        self.layout = layout
        self.rules = rules
        self.missions: dict[str, Mission] = {}
        self.robot_queues: dict[str, list[str]] = {}
        self.robot_locations: dict[str, str] = dict(layout.robot_homes)
        self.robot_location_at: dict[str, str] = {}
        self.zone_states: dict[str, SegmentState] = {
            zone_id: SegmentState() for zone_id in layout.zones
        }
        self.connector_states: dict[str, SegmentState] = {
            connector_id: SegmentState() for connector_id in layout.connectors
        }
        self.reservations: dict[str, Reservation] = {}
        self.applied_event_ids: set[str] = set()
        self.receipts: dict[str, dict[str, Any]] = {}
        self.now: str | None = None
        self._res_seq = 0

    # ------------------------------------------------------------------
    # 事件应用
    # ------------------------------------------------------------------

    def apply(self, event: EventEnvelope) -> list[str]:
        """应用一个事件，返回需要提示给值班人员的说明。"""

        if event.event_id in self.applied_event_ids:
            return [f"重复事件 {event.event_id} 已忽略"]
        self.applied_event_ids.add(event.event_id)
        if self.now is None or _parse(event.received_at) > _parse(self.now):
            self.now = event.received_at

        handler = {
            "mission_created": self._on_mission_created,
            "zone_closed": self._on_zone_closed,
            "zone_reopened": self._on_zone_reopened,
            "zone_contaminated": self._on_zone_contaminated,
            "cleaning_recorded": self._on_cleaning,
            "door_result": self._on_door_result,
            "robot_arrived": self._on_robot_arrived,
            "temp_excursion": self._on_temp_excursion,
            "manual_override": self._on_manual_override,
        }.get(event.kind)
        if handler is None:
            notes = [f"未知事件类型 {event.kind}，已原样存档"]
        else:
            notes = handler(event)
        self._replan()
        return notes

    # ------------------------------------------------------------------
    # 各类事件处理
    # ------------------------------------------------------------------

    def _on_mission_created(self, event: EventEnvelope) -> list[str]:
        attrs = event.attributes
        robot_id = str(attrs.get("robot_id", ""))
        payload = str(attrs.get("payload_class", ""))
        destination = str(attrs.get("zone") or attrs.get("destination") or "")
        sequence = int(attrs.get("sequence", 0))
        mission_id = str(attrs.get("mission_id") or f"{robot_id}#{sequence}" or event.event_id)
        notes: list[str] = []
        if mission_id in self.missions:
            return [f"任务 {mission_id} 已存在，创建事件 {event.event_id} 仅存档"]
        limit = attrs.get("temp_limit_minutes")
        if limit is None:
            limit = self.rules.temp_limit_for(payload)
        origin = str(attrs.get("origin") or self.robot_locations.get(robot_id) or "")
        mission = Mission(
            mission_id=mission_id,
            robot_id=robot_id,
            payload_class=payload,
            origin=origin,
            destination=destination,
            sequence=sequence,
            created_at=event.occurred_at,
            created_event=event.event_id,
            temp_limit_minutes=int(limit) if limit is not None else None,
        )
        self.missions[mission_id] = mission
        queue = self.robot_queues.setdefault(robot_id, [])
        queue.append(mission_id)
        queue.sort(key=lambda mid: (self.missions[mid].sequence, self.missions[mid].created_at, mid))
        if robot_id not in self.robot_locations:
            notes.append(f"机器人 {robot_id} 未在拓扑中登记，任务将无法放行")
        if not origin:
            notes.append(f"任务 {mission_id} 缺少起点区域")
        return notes

    def _on_zone_closed(self, event: EventEnvelope) -> list[str]:
        zone_id = str(event.attributes.get("zone", ""))
        state = self.zone_states.get(zone_id)
        if state is None:
            return [f"封控事件指向未知区域 {zone_id}"]
        state.closed = True
        state.closed_by = event.event_id
        return [f"区域 {zone_id} 已封控（{event.event_id}）"]

    def _on_zone_reopened(self, event: EventEnvelope) -> list[str]:
        zone_id = str(event.attributes.get("zone", ""))
        state = self.zone_states.get(zone_id)
        if state is None:
            return [f"解封事件指向未知区域 {zone_id}"]
        state.closed = False
        state.closed_by = None
        return [f"区域 {zone_id} 已解封（{event.event_id}）"]

    def _on_zone_contaminated(self, event: EventEnvelope) -> list[str]:
        target, state = self._target_state(event)
        if state is None:
            return [f"污染事件指向未知目标: {event.attributes}"]
        state.contaminated_since = event.occurred_at
        state.contamination_source = {
            "event_id": event.event_id,
            "reported_by": event.attributes.get("source", "external"),
        }
        return [f"{target} 已标记污染（{event.event_id}）"]

    def _on_cleaning(self, event: EventEnvelope) -> list[str]:
        target, state = self._target_state(event)
        if state is None:
            return [f"清洁事件指向未知目标: {event.attributes}"]
        attrs = event.attributes
        cert_id = str(attrs.get("cert_id", ""))
        expires = attrs.get("cert_expires_at")
        issued = attrs.get("cert_issued_at")
        if expires is None and issued is not None:
            expires = _iso(_parse(str(issued)) + timedelta(minutes=self.rules.cert_valid_minutes))
        record = {
            "event_id": event.event_id,
            "target": target,
            "cert_id": cert_id,
            "occurred_at": event.occurred_at,
            "cert_expires_at": expires,
            "valid": False,
            "reason": "",
        }
        if expires is not None and _parse(event.occurred_at) > _parse(str(expires)):
            record["reason"] = "cert_expired"
            state.cleanings.append(record)
            return [f"清洁凭证 {cert_id} 在 {event.occurred_at} 已过期，{target} 的清场不被接受"]
        if state.contaminated_since is None:
            record["valid"] = True
            record["reason"] = "no_contamination"
            state.cleanings.append(record)
            return [f"{target} 当前无污染记录，清洁事件 {event.event_id} 已存档"]
        if _parse(event.occurred_at) < _parse(state.contaminated_since):
            record["reason"] = "stale"
            state.cleanings.append(record)
            return [
                f"补传清洁 {event.event_id} 发生于污染之前，不能清除 {target} 的污染"
            ]
        record["valid"] = True
        state.cleanings.append(record)
        state.contaminated_since = None
        state.contamination_source = None
        return [f"{target} 已凭 {cert_id} 清场（{event.event_id}）"]

    def _on_door_result(self, event: EventEnvelope) -> list[str]:
        attrs = event.attributes
        receipt_id = str(attrs.get("receipt_id") or event.event_id)
        if receipt_id in self.receipts:
            return [f"门禁回执 {receipt_id} 重复送达，已忽略（事件 {event.event_id}）"]
        notes: list[str] = []
        decision = attrs.get("decision")
        if decision not in ("granted", "denied"):
            decision = "denied"
            notes.append(f"门禁回执 {receipt_id} 缺少明确结论，按拒绝处理")
        door = str(attrs.get("door") or f"door:{attrs.get('zone', '')}")
        receipt = {
            "receipt_id": receipt_id,
            "door": door,
            "decision": decision,
            "event_id": event.event_id,
            "occurred_at": event.occurred_at,
        }
        self.receipts[receipt_id] = receipt
        mission = self._mission_for_event(event)
        if mission is None:
            notes.append(f"门禁回执 {receipt_id} 找不到关联任务，仅存档")
            return notes
        if mission.status in FINAL_STATUSES:
            self._file_late(mission, event, "门禁回执到达时任务已终结")
            return notes + [f"任务 {mission.mission_id} 已签认/取消，回执 {receipt_id} 仅归档"]
        mission.door_events.append(receipt)
        if decision == "denied":
            self._pause(
                mission,
                PAUSE_DOOR_DENIED,
                f"门禁 {door} 拒绝通行（回执 {receipt_id}）",
                evidence=[event.event_id],
                at=event.occurred_at,
            )
            notes.append(f"任务 {mission.mission_id} 因门禁拒绝进入暂停，等待人工处置")
        return notes

    def _on_robot_arrived(self, event: EventEnvelope) -> list[str]:
        attrs = event.attributes
        robot_id = str(attrs.get("robot_id", ""))
        zone_id = str(attrs.get("zone", ""))
        notes: list[str] = []
        last_at = self.robot_location_at.get(robot_id)
        if last_at is None or _parse(event.occurred_at) >= _parse(last_at):
            self.robot_locations[robot_id] = zone_id
            self.robot_location_at[robot_id] = event.occurred_at
        else:
            notes.append(f"补传到达 {event.event_id} 早于已知位置，未改动机器人当前位置")
        mission = self._mission_for_event(event)
        if mission is None:
            mission = self._last_final_mission(robot_id)
            if mission is not None:
                self._file_late(mission, event, "到达补传于任务签认之后")
                notes.append(f"任务 {mission.mission_id} 已签认，补传到达不倒改结论")
                return notes
            notes.append(f"到达事件 {event.event_id} 找不到关联任务")
            return notes
        if mission.status in FINAL_STATUSES:
            self._file_late(mission, event, "到达补传于任务签认之后")
            return notes + [f"任务 {mission.mission_id} 已签认，补传到达不倒改结论"]
        mission.arrivals.append(
            {"zone": zone_id, "occurred_at": event.occurred_at, "event_id": event.event_id}
        )
        if zone_id != mission.destination:
            return notes
        if mission.status == DISPATCHED:
            notes.extend(self._try_sign(mission, event.occurred_at, event.event_id, confirmed_by=None))
        else:
            notes.append(
                f"任务 {mission.mission_id} 处于 {mission.status}，到达记录已归档，需人工确认"
            )
        return notes

    def _on_temp_excursion(self, event: EventEnvelope) -> list[str]:
        mission = self._mission_for_event(event)
        if mission is None:
            return [f"温控事件 {event.event_id} 找不到关联任务"]
        if mission.status in FINAL_STATUSES:
            self._file_late(mission, event, "温控告警到达时任务已终结")
            return [f"任务 {mission.mission_id} 已签认，温控告警仅归档"]
        observed = event.attributes.get("observed_c", "未知")
        self._pause(
            mission,
            PAUSE_TEMP_TIMEOUT,
            f"载荷温度越限（观测 {observed}℃）",
            evidence=[event.event_id],
            at=event.occurred_at,
        )
        return [f"任务 {mission.mission_id} 因温控越限暂停，等待人工处置"]

    def _on_manual_override(self, event: EventEnvelope) -> list[str]:
        attrs = event.attributes
        mission_id = str(attrs.get("mission_id", ""))
        mission = self.missions.get(mission_id)
        if mission is None:
            return [f"人工接管指向未知任务 {mission_id}"]
        action = str(attrs.get("action", ""))
        record = {
            "event_id": event.event_id,
            "action": action,
            "operator": str(attrs.get("operator", "")),
            "reason": str(attrs.get("reason", "")),
            "at": event.occurred_at,
        }
        mission.overrides.append(record)
        if action == "resume":
            if mission.status != PAUSED:
                return [f"任务 {mission_id} 不在暂停态，接管 {event.event_id} 仅记录"]
            mission.status = PENDING
            mission.pause_reasons = []
            notes = [f"任务 {mission_id} 由 {record['operator']} 人工恢复：{record['reason']}"]
            notes.extend(self._sign_from_filed_arrival(mission, event.event_id))
            return notes
        if action == "cancel":
            if mission.status in FINAL_STATUSES:
                return [f"任务 {mission_id} 已终结，取消无效"]
            self._release_future_reservations(mission)
            mission.status = CANCELLED
            return [f"任务 {mission_id} 由 {record['operator']} 人工取消：{record['reason']}"]
        return [f"未知接管动作 {action}"]

    # ------------------------------------------------------------------
    # 内部：目标解析与任务关联
    # ------------------------------------------------------------------

    def _target_state(self, event: EventEnvelope) -> tuple[str, SegmentState | None]:
        attrs = event.attributes
        connector_id = attrs.get("connector")
        if connector_id:
            return str(connector_id), self.connector_states.get(str(connector_id))
        zone_id = str(attrs.get("zone", ""))
        return zone_id, self.zone_states.get(zone_id)

    def _mission_for_event(self, event: EventEnvelope) -> Mission | None:
        attrs = event.attributes
        mission_id = attrs.get("mission_id")
        if mission_id and str(mission_id) in self.missions:
            return self.missions[str(mission_id)]
        robot_id = str(attrs.get("robot_id", ""))
        return self._head_mission(robot_id)

    def _head_mission(self, robot_id: str) -> Mission | None:
        for mission_id in self.robot_queues.get(robot_id, []):
            mission = self.missions[mission_id]
            if mission.status in ACTIVE_STATUSES:
                return mission
        return None

    def _last_final_mission(self, robot_id: str) -> Mission | None:
        for mission_id in reversed(self.robot_queues.get(robot_id, [])):
            mission = self.missions[mission_id]
            if mission.status in FINAL_STATUSES:
                return mission
        return None

    def _file_late(self, mission: Mission, event: EventEnvelope, note: str) -> None:
        mission.late_events.append(
            {"event_id": event.event_id, "kind": event.kind, "occurred_at": event.occurred_at, "note": note}
        )

    def _pause(
        self,
        mission: Mission,
        code: str,
        detail: str,
        evidence: list[str],
        at: str,
    ) -> None:
        if mission.status in FINAL_STATUSES:
            return
        if mission.status == PAUSED and any(r["code"] == code for r in mission.pause_reasons):
            return
        self._release_future_reservations(mission)
        mission.status = PAUSED
        mission.pause_reasons.append(
            {"code": code, "detail": detail, "evidence": list(evidence), "at": at}
        )

    def _release_future_reservations(self, mission: Mission) -> None:
        """释放尚未完全过去的预留窗口；已结束的窗口保留为历史事实。"""

        now = _parse(self.now) if self.now else None
        for reservation_id in mission.reservations:
            reservation = self.reservations[reservation_id]
            if reservation.released:
                continue
            if now is None or _parse(reservation.end) > now:
                reservation.released = True

    # ------------------------------------------------------------------
    # 签认
    # ------------------------------------------------------------------

    def _try_sign(
        self,
        mission: Mission,
        occurred_at: str,
        event_id: str,
        confirmed_by: str | None,
    ) -> list[str]:
        notes: list[str] = []
        destination = mission.destination
        zone_state = self.zone_states.get(destination)
        if zone_state is not None and zone_state.closed:
            notes.append(f"目的区域 {destination} 处于封控，任务 {mission.mission_id} 不能签认")
            return notes
        grade = self.layout.zones[destination].grade if destination in self.layout.zones else ""
        granted = [r for r in mission.door_events if r["decision"] == "granted"]
        if grade == "isolation" and not granted:
            notes.append(f"进入隔离区 {destination} 缺少门禁放行回执，等待门禁结果")
            return notes
        deadline = mission.temp_deadline()
        if deadline is not None and _parse(occurred_at) > _parse(deadline):
            self._pause(
                mission,
                PAUSE_TEMP_TIMEOUT,
                f"到达时间 {occurred_at} 超过温控时限 {deadline}",
                evidence=[event_id],
                at=occurred_at,
            )
            notes.append(f"任务 {mission.mission_id} 温控超时，暂停等待人工处置")
            return notes
        mission.status = DELIVERED
        mission.delivered_at = occurred_at
        mission.signed = {
            "signed_at": occurred_at,
            "sign_event": event_id,
            "confirmed_by_override": confirmed_by,
            "door_receipts": [dict(r) for r in mission.door_events],
            "cleaning_evidence": [dict(c) for c in mission.cleaning_evidence],
            "temp": {
                "limit_minutes": mission.temp_limit_minutes,
                "deadline": deadline,
                "within_limit": True,
            },
            "route": list(mission.route),
            "reservations": list(mission.reservations),
            "rules_version": mission.rules_version or self.rules.version,
        }
        notes.append(f"任务 {mission.mission_id} 已于 {occurred_at} 签认")
        return notes

    def _sign_from_filed_arrival(self, mission: Mission, override_event: str) -> list[str]:
        """人工恢复时，若归档的到达记录满足签认条件，则据此补签。

        只采纳温控时限内最早的目的地到达；没有合格到达时保持原状，
        由重计划按当前时间重新判定（通常会因温控超时再次暂停）。
        """

        deadline = mission.temp_deadline()
        candidates = [a for a in mission.arrivals if a["zone"] == mission.destination]
        candidates.sort(key=lambda a: a["occurred_at"])
        for arrival in candidates:
            if deadline is not None and _parse(arrival["occurred_at"]) > _parse(deadline):
                continue
            notes = self._try_sign(
                mission, arrival["occurred_at"], arrival["event_id"], confirmed_by=override_event
            )
            if mission.status == DELIVERED:
                return [f"依据归档到达 {arrival['event_id']} 补签任务 {mission.mission_id}"] + notes
        return []

    # ------------------------------------------------------------------
    # 计划与预留
    # ------------------------------------------------------------------

    def _replan(self) -> None:
        now = self.now
        if now is None:
            return
        # 1. 温控超时检查（只暂停，不绕过）
        for mission in self.missions.values():
            if mission.status in (PENDING, DISPATCHED):
                deadline = mission.temp_deadline()
                if deadline is not None and _parse(deadline) < _parse(now):
                    self._pause(
                        mission,
                        PAUSE_TEMP_TIMEOUT,
                        f"超过温控时限 {deadline} 仍未签认",
                        evidence=[mission.created_event],
                        at=now,
                    )
        # 2. 凭证过期检查：唯一阻断原因是过期凭证的污染时，转入人工处置
        for mission in self.missions.values():
            if mission.status not in (PENDING, DISPATCHED):
                continue
            _, _, reasons = self._evaluate(mission, now)
            if any(reason["code"] == PAUSE_CERT_EXPIRED for reason in reasons):
                expired = [r for r in reasons if r["code"] == PAUSE_CERT_EXPIRED][0]
                self._pause(
                    mission,
                    PAUSE_CERT_EXPIRED,
                    expired["detail"],
                    evidence=expired["evidence"],
                    at=now,
                )
        # 3. 空闲机器人尝试放行队首任务
        for robot_id in sorted(self.robot_queues):
            head = self._head_mission(robot_id)
            if head is None or head.status != PENDING:
                continue
            if self.robot_locations.get(robot_id) != head.origin:
                continue
            path, plan, _ = self._evaluate(head, now)
            if path is None or plan is None:
                continue
            self._dispatch(head, path, plan, now)

    def _evaluate(
        self, mission: Mission, now: str
    ) -> tuple[list[str] | None, list[tuple[str, datetime, datetime]] | None, list[dict[str, Any]]]:
        """评估任务当前可走的路线；返回 (路径, 排程, 阻断依据)。"""

        if mission.origin not in self.layout.zones or mission.destination not in self.layout.zones:
            return None, None, [
                {
                    "code": "no_route",
                    "detail": f"起点或终点未知（{mission.origin} -> {mission.destination}）",
                    "target": mission.destination,
                    "evidence": [],
                }
            ]
        reasons: list[dict[str, Any]] = []
        for path in self.layout.paths(mission.origin, mission.destination):
            blockers = self._route_blockers(path, mission)
            if blockers:
                reasons.extend(blockers)
                continue
            plan = self._schedule(path, now)
            if plan is None:
                reasons.append(
                    {
                        "code": "slot_unavailable",
                        "detail": "预留窗口在排程 horizon 内已满",
                        "target": ",".join(path),
                        "evidence": [],
                    }
                )
                continue
            return path, plan, []
        return None, None, _dedup_reasons(reasons)

    def _route_blockers(self, path: list[str], mission: Mission) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        try:
            cleanliness = self.rules.cleanliness_of(mission.payload_class)
        except Exception:
            return [
                {
                    "code": "unknown_payload",
                    "detail": f"未知载荷类型 {mission.payload_class}",
                    "target": mission.payload_class,
                    "evidence": [],
                }
            ]
        zone_seq = self.layout.zones_along(mission.origin, path)
        for connector_id in path:
            connector = self.layout.connectors[connector_id]
            if (
                connector.payload_classes is not None
                and mission.payload_class not in connector.payload_classes
            ):
                code = "elevator_capability" if connector.kind == "elevator" else "payload_not_allowed"
                reasons.append(
                    {
                        "code": code,
                        "detail": f"{connector_id} 不承运载荷 {mission.payload_class}",
                        "target": connector_id,
                        "evidence": [],
                    }
                )
            if connector.grade not in self.rules.connector_grade_access[cleanliness]:
                reasons.append(
                    {
                        "code": "grade_forbidden",
                        "detail": f"{cleanliness} 载荷不得使用 {connector.grade} 通道 {connector_id}",
                        "target": connector_id,
                        "evidence": [],
                    }
                )
            reasons.extend(self._segment_blockers(self.connector_states[connector_id], connector_id, cleanliness))
        for zone_id in zone_seq[1:]:
            zone_state = self.zone_states[zone_id]
            if zone_state.closed:
                reasons.append(
                    {
                        "code": "zone_closed",
                        "detail": f"区域 {zone_id} 临时封控",
                        "target": zone_id,
                        "evidence": [zone_state.closed_by] if zone_state.closed_by else [],
                    }
                )
            zone_grade = self.layout.zones[zone_id].grade
            if zone_grade not in self.rules.zone_grade_access[cleanliness]:
                reasons.append(
                    {
                        "code": "grade_forbidden",
                        "detail": f"{cleanliness} 载荷不得进入 {zone_grade} 区域 {zone_id}",
                        "target": zone_id,
                        "evidence": [],
                    }
                )
            reasons.extend(self._segment_blockers(zone_state, zone_id, cleanliness))
        return reasons

    def _segment_blockers(
        self, state: SegmentState, target: str, cleanliness: str
    ) -> list[dict[str, Any]]:
        if state.contaminated_since is None or cleanliness == "dirty":
            return []
        latest = state.cleanings[-1] if state.cleanings else None
        if latest is not None and not latest["valid"] and latest["reason"] == "cert_expired":
            return [
                {
                    "code": PAUSE_CERT_EXPIRED,
                    "detail": f"{target} 的清场凭证 {latest['cert_id']} 已过期，需人工处置",
                    "target": target,
                    "evidence": [latest["event_id"]],
                }
            ]
        source = state.contamination_source or {}
        return [
            {
                "code": "contaminated",
                "detail": f"{target} 自 {state.contaminated_since} 起污染，等待清场",
                "target": target,
                "evidence": [source.get("event_id")] if source.get("event_id") else [],
            }
        ]

    def _overlap_count(self, connector_id: str, start: datetime, end: datetime) -> int:
        count = 0
        for reservation in self.reservations.values():
            if reservation.resource_id != connector_id or reservation.released:
                continue
            if _parse(reservation.start) < end and start < _parse(reservation.end):
                count += 1
        return count

    def _schedule(
        self, path: list[str], now: str
    ) -> list[tuple[str, datetime, datetime]] | None:
        cursor = _parse(now)
        horizon = cursor + timedelta(minutes=self.rules.slot_horizon_minutes)
        step = timedelta(seconds=self.rules.slot_step_seconds)
        plan: list[tuple[str, datetime, datetime]] = []
        for connector_id in path:
            connector = self.layout.connectors[connector_id]
            duration = timedelta(seconds=connector.traverse_seconds)
            start = cursor
            while self._overlap_count(connector_id, start, start + duration) >= connector.capacity:
                start += step
                if start + duration > horizon:
                    return None
            plan.append((connector_id, start, start + duration))
            cursor = start + duration
        return plan

    def _dispatch(
        self,
        mission: Mission,
        path: list[str],
        plan: list[tuple[str, datetime, datetime]],
        now: str,
    ) -> None:
        reservation_ids: list[str] = []
        for connector_id, start, end in plan:
            self._res_seq += 1
            reservation_id = f"res-{self._res_seq:04d}"
            self.reservations[reservation_id] = Reservation(
                reservation_id=reservation_id,
                resource_id=connector_id,
                mission_id=mission.mission_id,
                robot_id=mission.robot_id,
                start=_iso(start),
                end=_iso(end),
                rules_version=self.rules.version,
            )
            reservation_ids.append(reservation_id)
        mission.route = list(path)
        mission.reservations = reservation_ids
        mission.status = DISPATCHED
        mission.dispatched_at = now
        mission.rules_version = self.rules.version
        mission.cleaning_evidence = [
            dict(record)
            for connector_id in path
            for record in self.connector_states[connector_id].cleanings
            if record["valid"] and record["reason"] != "no_contamination"
        ]
        if self.rules.cleanliness_of(mission.payload_class) == "dirty":
            for connector_id in path:
                state = self.connector_states[connector_id]
                state.contaminated_since = now
                state.contamination_source = {
                    "event_id": mission.created_event,
                    "mission_id": mission.mission_id,
                    "note": "污染载荷通行",
                }

    # ------------------------------------------------------------------
    # 值班视图与交付记录
    # ------------------------------------------------------------------

    def _blocking_for(self, mission: Mission, now: str) -> list[dict[str, Any]]:
        if mission.status == DISPATCHED and mission.route:
            return _dedup_reasons(self._route_blockers(mission.route, mission))
        _, _, reasons = self._evaluate(mission, now)
        if self.robot_locations.get(mission.robot_id) != mission.origin:
            reasons = [
                {
                    "code": "robot_not_at_origin",
                    "detail": f"机器人不在起点 {mission.origin}",
                    "target": mission.origin,
                    "evidence": [],
                }
            ] + reasons
        return reasons

    def next_action(self, robot_id: str, now: str) -> dict[str, Any]:
        head = self._head_mission(robot_id)
        if head is None:
            return {"kind": "idle", "detail": "无待办任务"}
        if head.status == PAUSED:
            return {
                "kind": "paused",
                "mission_id": head.mission_id,
                "detail": "任务暂停，需人工处置（override resume/cancel）",
                "pause_reasons": [dict(r) for r in head.pause_reasons],
                "blocking": self._blocking_for(head, now),
            }
        if head.status == DISPATCHED:
            blocking = self._blocking_for(head, now)
            reservations = [
                self.reservations[rid].as_dict()
                for rid in head.reservations
                if not self.reservations[rid].released
            ]
            if blocking:
                return {
                    "kind": "hold",
                    "mission_id": head.mission_id,
                    "detail": "已放行但前方受阻，保持等待",
                    "blocking": blocking,
                    "reservations": reservations,
                }
            return {
                "kind": "proceed",
                "mission_id": head.mission_id,
                "detail": "按预留窗口通行",
                "route": list(head.route),
                "reservations": reservations,
            }
        blocking = self._blocking_for(head, now)
        return {
            "kind": "wait",
            "mission_id": head.mission_id,
            "detail": "等待合法路线",
            "blocking": blocking,
        }

    def contamination_view(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        exposed: dict[str, dict[str, Any]] = {}

        def mark_exposed(zone_id: str, via: str, source: dict[str, Any], since: str) -> None:
            if zone_id not in self.zone_states:
                return
            if self.zone_states[zone_id].contaminated_since is not None:
                return
            current = exposed.get(zone_id)
            if current is None or since < current["since"]:
                exposed[zone_id] = {
                    "target": zone_id,
                    "level": "exposed",
                    "since": since,
                    "source": source,
                    "path": [via, zone_id],
                }

        targets = list(self.connector_states.items()) + list(self.zone_states.items())
        for target, state in targets:
            if state.contaminated_since is None:
                continue
            source = dict(state.contamination_source or {})
            entries.append(
                {
                    "target": target,
                    "level": "contaminated",
                    "since": state.contaminated_since,
                    "source": source,
                    "path": [target],
                }
            )
        for target, state in targets:
            if state.contaminated_since is None:
                continue
            source = dict(state.contamination_source or {})
            since = state.contaminated_since
            if target in self.connector_states:
                for endpoint in self.layout.connectors[target].endpoints:
                    mark_exposed(endpoint, target, source, since)
            else:
                for connector_id in self.layout.connectors_of(target):
                    neighbor = self.layout.other_end(connector_id, target)
                    mark_exposed(neighbor, connector_id, source, since)
        entries.extend(exposed.values())
        entries.sort(key=lambda item: (item["level"] != "contaminated", item["target"]))
        return entries

    def view(self, at: str | None = None) -> dict[str, Any]:
        now = at or self.now
        robots = []
        for robot_id in sorted(set(self.robot_queues) | set(self.robot_locations)):
            queue = [
                {"mission_id": mid, "status": self.missions[mid].status}
                for mid in self.robot_queues.get(robot_id, [])
            ]
            robots.append(
                {
                    "robot_id": robot_id,
                    "location": self.robot_locations.get(robot_id),
                    "queue": queue,
                    "next_action": self.next_action(robot_id, now) if now else {"kind": "idle"},
                }
            )
        return {
            "generated_at": now,
            "rules_version": self.rules.version,
            "layout_id": self.layout.layout_id,
            "robots": robots,
            "missions": {mid: m.summary() for mid, m in sorted(self.missions.items())},
            "contamination": self.contamination_view(),
            "reservations": [
                r.as_dict() for r in sorted(self.reservations.values(), key=lambda r: r.reservation_id)
            ],
        }

    def delivery_record(self, mission_id: str) -> dict[str, Any]:
        mission = self.missions.get(mission_id)
        if mission is None:
            raise KeyError(f"未知任务 {mission_id}")
        record = mission.summary()
        record.update(
            {
                "door_events": [dict(r) for r in mission.door_events],
                "arrivals": [dict(a) for a in mission.arrivals],
                "cleaning_evidence": [dict(c) for c in mission.cleaning_evidence],
                "late_events": [dict(e) for e in mission.late_events],
                "signed": dict(mission.signed) if mission.signed else None,
            }
        )
        return record


def _dedup_reasons(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for reason in reasons:
        key = (reason["code"], reason.get("target", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(reason)
    return result
