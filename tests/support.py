"""测试共享支撑：小型拓扑/规则/机器人与事件构造。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hospital_route.contracts import EventEnvelope  # noqa: E402
from hospital_route.service import HospitalRouteService  # noqa: E402

TOPOLOGY = {
    "zones": [
        {"zone_id": "A", "name": "药房", "kind": "clean", "floor": "F1",
         "adjacency": [["e1"]], "elevator_banks": []},
        {"zone_id": "B", "name": "缓冲间", "kind": "buffer", "floor": "F1",
         "adjacency": [["e1"], ["e2"], ["e3"]], "elevator_banks": ["lift-x"]},
        {"zone_id": "C", "name": "污物间", "kind": "dirty", "floor": "F1",
         "adjacency": [["e2"]], "elevator_banks": []},
        {"zone_id": "D", "name": "二层电梯厅", "kind": "clean", "floor": "F2",
         "adjacency": [["e3"], ["e4"]], "elevator_banks": ["lift-x"]},
        {"zone_id": "E", "name": "隔离病房", "kind": "dirty", "floor": "F2",
         "adjacency": [["e4"]], "elevator_banks": []},
    ],
    "edges": [
        {"edge_id": "e1", "a": "A", "b": "B", "kind": "corridor", "base_cost": 10, "capacity": 2},
        {"edge_id": "e2", "a": "B", "b": "C", "kind": "narrow", "base_cost": 10, "capacity": 1,
         "door_id": "door-1"},
        {"edge_id": "e3", "a": "B", "b": "D", "kind": "elevator", "base_cost": 20, "capacity": 1,
         "elevator_id": "lift-x"},
        {"edge_id": "e4", "a": "D", "b": "E", "kind": "corridor", "base_cost": 10, "capacity": 2},
    ],
    "elevators": [
        {"elevator_id": "lift-x", "edge_id": "e3", "serves": ["B", "D"], "capacity": 1,
         "allowed_payloads": ["medicine", "specimen"], "cycle_cost": 20},
    ],
}

RULES = {
    "rule_version": "test-v1",
    "payload_classes": ["medicine", "specimen", "linen_clean", "waste"],
    "payload_tiers": {"medicine": 0, "specimen": 1, "linen_clean": 0, "waste": 2},
    "contamination_tiers": {"clean": 0, "suspected": 1, "confirmed": 2},
    "contamination_spread": {"confirmed": ["suspected"]},
    "cleaning_valid_seconds": 3600,
    "temp_limits": {"medicine": 1000, "specimen": 500, "linen_clean": None, "waste": None},
    "requires_certificate": ["specimen"],
    "elevator_clean_min_tier": 0,
}

ROBOTS = [
    {"robot_id": "r1", "home_zone": "A", "max_tier": 1, "elevator_access": ["lift-x"],
     "payload_classes": ["medicine", "specimen"]},
    {"robot_id": "r2", "home_zone": "E", "max_tier": 2, "elevator_access": [],
     "payload_classes": ["waste"]},
]

T0 = "2026-09-22T20:00:00+08:00"


def evt(kind: str, event_id: str, occurred: str, received: str | None = None, **attrs) -> EventEnvelope:
    return EventEnvelope.from_dict(
        {
            "event_id": event_id,
            "kind": kind,
            "occurred_at": occurred,
            "received_at": received or occurred,
            "attributes": attrs,
        }
    )


def mission_evt(event_id: str, mission_id: str, robot: str, payload: str,
                origin: str, destination: str, occurred: str = T0,
                received: str | None = None) -> EventEnvelope:
    return evt(
        "mission_created", event_id, occurred, received,
        mission_id=mission_id, robot_id=robot, payload_class=payload,
        origin=origin, destination=destination,
    )


def make_service(tmp: str | Path, scenario: str = "test") -> HospitalRouteService:
    base = Path(tmp)
    base.mkdir(parents=True, exist_ok=True)
    (base / "topology.json").write_text(json.dumps(TOPOLOGY), encoding="utf-8")
    (base / "rules.json").write_text(json.dumps(RULES), encoding="utf-8")
    (base / "robots.json").write_text(json.dumps(ROBOTS), encoding="utf-8")
    return HospitalRouteService.load(base, scenario=scenario)
