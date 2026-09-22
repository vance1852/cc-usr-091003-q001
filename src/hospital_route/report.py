"""值班视图与交付记录。

- next_actions：每台机器人下一项合法动作；
- blockers：路线被阻断的具体依据（含封控、污染、凭证、容量证据）；
- risk：污染如何传播到关联区域；
- delivery_record：单次交付的完整证据链（门禁、清场、温控、规则版本）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .engine import Engine
from .state import SystemState


def build_next_actions(engine: Engine, state: SystemState, now: datetime) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "rule_version": state.rule_version,
        "robots": engine.next_actions(state, now),
    }


def build_blockers(engine: Engine, state: SystemState, now: datetime) -> dict[str, Any]:
    """汇总当前所有未结任务的阻断依据。"""

    items: list[dict[str, Any]] = []
    for mission_id in sorted(state.missions):
        mission = state.missions[mission_id]
        if mission.is_final:
            continue
        if mission.status == "suspended":
            items.append(
                {
                    "mission_id": mission_id,
                    "robot_id": mission.robot_id,
                    "status": mission.status,
                    "reasons": [
                        {
                            "code": reason["code"],
                            "message": reason["message"],
                            "evidence": reason.get("evidence", {}),
                        }
                        for reason in mission.hold_reasons
                    ],
                }
            )
            continue
        _candidate, reasons = engine.find_route(state, mission, now, ignore_mission=mission_id)
        if reasons:
            items.append(
                {
                    "mission_id": mission_id,
                    "robot_id": mission.robot_id,
                    "status": mission.status,
                    "reasons": [
                        {"code": r.code, "message": r.message, "evidence": r.evidence} for r in reasons
                    ],
                }
            )
    return {"generated_at": now.isoformat(), "rule_version": state.rule_version, "blocked": items}


def build_risk_report(state: SystemState) -> dict[str, Any]:
    """污染分布与传播链：每个污染目标可追溯到来源事件与路径。"""

    propagation: list[dict[str, Any]] = []
    for entry in state.audit:
        if entry.kind == "contamination_propagated":
            propagation.append(
                {
                    "at": entry.at,
                    "source": {"kind": entry.data["source_kind"], "id": entry.data["source_id"]},
                    "level": entry.data["level"],
                    "cause": entry.data["cause"],
                    "targets": entry.data["targets"],
                    "evidence": entry.data.get("evidence", {}),
                }
            )
    zones = {
        zone_id: level
        for zone_id, level in sorted(state.zone_contamination.items())
        if level != "clean"
    }
    elevators = {
        elevator_id: level
        for elevator_id, level in sorted(state.elevator_contamination.items())
        if level != "clean"
    }
    return {
        "generated_at": state.last_clock,
        "rule_version": state.rule_version,
        "contaminated_zones": zones,
        "contaminated_elevators": elevators,
        "propagation": propagation,
    }


def build_delivery_record(state: SystemState, mission_id: str) -> dict[str, Any]:
    """交付记录：任务历史、预留、门禁回执、清场与温控证据、人工接管。"""

    mission = state.missions.get(mission_id)
    if mission is None:
        raise KeyError(f"未知任务 {mission_id}")
    reservations = [
        {
            "reservation_id": r.reservation_id,
            "resource_id": r.resource_id,
            "resource_kind": r.resource_kind,
            "direction": r.direction,
            "start": r.start,
            "end": r.end,
            "status": r.status,
        }
        for r in state.reservations.values()
        if r.mission_id == mission_id
    ]
    reservations.sort(key=lambda item: (item["start"], item["reservation_id"]))
    door_events = [
        entry
        for entry in state.audit
        if entry.kind == "door_result"
        and entry.data.get("mission_id") == mission_id
    ]
    cleaning_evidence = [
        {
            "certificate_id": cert.certificate_id,
            "target": cert.target,
            "valid_from": cert.valid_from,
            "valid_until": cert.valid_until,
            "status": cert.status,
        }
        for cert in state.certificates.values()
    ]
    cleaning_evidence.sort(key=lambda item: item["certificate_id"])
    takeovers = [
        {
            "takeover_id": t.takeover_id,
            "at": t.at,
            "operator": t.operator,
            "action": t.action,
            "note": t.note,
            "details": t.details,
        }
        for t in state.takeovers
        if t.mission_id == mission_id
    ]
    return {
        "mission_id": mission_id,
        "robot_id": mission.robot_id,
        "payload_class": mission.payload_class,
        "origin": mission.origin,
        "destination": mission.destination,
        "status": mission.status,
        "rule_version": state.rule_version,
        "created_at": mission.created_at,
        "finalized_at": mission.finalized_at,
        "history": mission.history,
        "evidence": mission.evidence,
        "reservations": reservations,
        "door_results": [
            {"at": entry.at, "event_id": entry.event_id, "data": entry.data} for entry in door_events
        ],
        "cleaning_certificates": cleaning_evidence,
        "takeovers": takeovers,
    }
