"""状态持久化：JSON 快照。

快照包含任务、预留、封控、消毒凭证、门禁回执、机器人状态、污染档位、
采用的规则版本与每次人工接管记录；服务重启后按快照完整恢复。
持久化文件不进入版本库（见 .gitignore）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import ContractError
from .state import (
    AuditEntry,
    CleaningCert,
    Closure,
    DoorRecord,
    Mission,
    Rejection,
    Reservation,
    RobotState,
    SystemState,
    TakeoverRecord,
)

SNAPSHOT_VERSION = 1


def state_to_dict(state: SystemState) -> dict[str, Any]:
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "scenario": state.scenario,
        "rule_version": state.rule_version,
        "missions": {key: asdict(value) for key, value in state.missions.items()},
        "reservations": {key: asdict(value) for key, value in state.reservations.items()},
        "closures": {key: asdict(value) for key, value in state.closures.items()},
        "certificates": {key: asdict(value) for key, value in state.certificates.items()},
        "doors": {key: asdict(value) for key, value in state.doors.items()},
        "robots": {key: asdict(value) for key, value in state.robots.items()},
        "zone_contamination": dict(state.zone_contamination),
        "elevator_contamination": dict(state.elevator_contamination),
        "takeovers": [asdict(item) for item in state.takeovers],
        "audit": [asdict(item) for item in state.audit],
        "processed_events": sorted(state.processed_events),
        "rejections": [asdict(item) for item in state.rejections],
        "last_clock": state.last_clock,
    }


def state_from_dict(raw: dict[str, Any]) -> SystemState:
    if not isinstance(raw, dict):
        raise ContractError("快照必须是对象")
    if raw.get("snapshot_version") != SNAPSHOT_VERSION:
        raise ContractError("快照版本不受支持")
    state = SystemState(scenario=raw["scenario"], rule_version=raw["rule_version"])
    state.missions = {key: Mission(**value) for key, value in raw["missions"].items()}
    state.reservations = {key: Reservation(**value) for key, value in raw["reservations"].items()}
    state.closures = {key: Closure(**value) for key, value in raw["closures"].items()}
    state.certificates = {key: CleaningCert(**value) for key, value in raw["certificates"].items()}
    state.doors = {key: DoorRecord(**value) for key, value in raw["doors"].items()}
    state.robots = {key: RobotState(**value) for key, value in raw["robots"].items()}
    state.zone_contamination = dict(raw["zone_contamination"])
    state.elevator_contamination = dict(raw["elevator_contamination"])
    state.takeovers = [TakeoverRecord(**item) for item in raw["takeovers"]]
    state.audit = [AuditEntry(**item) for item in raw["audit"]]
    state.processed_events = set(raw["processed_events"])
    state.rejections = [Rejection(**item) for item in raw["rejections"]]
    state.last_clock = raw["last_clock"]
    return state


def save_state(state: SystemState, path: str | Path) -> None:
    """原子写入快照，避免崩溃留下半个文件。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state_to_dict(state), ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
    ) as handle:
        handle.write(payload)
        tmp_name = handle.name
    os.replace(tmp_name, target)


def load_state(path: str | Path) -> SystemState:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return state_from_dict(raw)
