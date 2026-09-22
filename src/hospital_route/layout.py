"""楼层拓扑：区域、连接通道与电梯能力。

拓扑只描述静态事实（区域洁污等级、通道容量、电梯可承运载荷），
动态状态（封控、污染、预留）由调度引擎维护。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import ContractError

ZONE_GRADES = ("clean", "shared", "isolation", "dirty")
CONNECTOR_GRADES = ("clean", "shared", "dirty")
CONNECTOR_KINDS = ("corridor", "elevator")


@dataclass(frozen=True)
class Zone:
    zone_id: str
    grade: str


@dataclass(frozen=True)
class Connector:
    connector_id: str
    kind: str
    endpoints: tuple[str, str]
    capacity: int
    traverse_seconds: int
    grade: str
    payload_classes: frozenset[str] | None  # None 表示不限制载荷


class Layout:
    """楼层拓扑图：区域为节点，走廊/电梯为边。"""

    def __init__(
        self,
        layout_id: str,
        zones: dict[str, Zone],
        connectors: dict[str, Connector],
        robot_homes: dict[str, str],
    ) -> None:
        self.layout_id = layout_id
        self.zones = zones
        self.connectors = connectors
        self.robot_homes = robot_homes
        self._by_zone: dict[str, list[str]] = {zone_id: [] for zone_id in zones}
        for connector in connectors.values():
            for endpoint in connector.endpoints:
                self._by_zone[endpoint].append(connector.connector_id)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Layout":
        if not isinstance(raw, dict) or not isinstance(raw.get("layout_id"), str):
            raise ContractError("拓扑文件缺少 layout_id")
        zones: dict[str, Zone] = {}
        for item in raw.get("zones", []):
            grade = item.get("grade")
            if grade not in ZONE_GRADES:
                raise ContractError(f"区域 {item.get('id')} 的 grade 非法: {grade}")
            zones[item["id"]] = Zone(zone_id=item["id"], grade=grade)
        if not zones:
            raise ContractError("拓扑文件缺少 zones")
        connectors: dict[str, Connector] = {}
        for item in raw.get("connectors", []):
            kind = item.get("kind")
            if kind not in CONNECTOR_KINDS:
                raise ContractError(f"通道 {item.get('id')} 的 kind 非法: {kind}")
            grade = item.get("grade")
            if grade not in CONNECTOR_GRADES:
                raise ContractError(f"通道 {item.get('id')} 的 grade 非法: {grade}")
            endpoints = item.get("endpoints")
            if (
                not isinstance(endpoints, list)
                or len(endpoints) != 2
                or any(end not in zones for end in endpoints)
            ):
                raise ContractError(f"通道 {item.get('id')} 的 endpoints 必须引用已知区域")
            capacity = int(item.get("capacity", 1))
            if capacity < 1:
                raise ContractError(f"通道 {item.get('id')} 的 capacity 必须为正")
            payload = item.get("payload_classes")
            connectors[item["id"]] = Connector(
                connector_id=item["id"],
                kind=kind,
                endpoints=(endpoints[0], endpoints[1]),
                capacity=capacity,
                traverse_seconds=int(item.get("traverse_seconds", 60)),
                grade=grade,
                payload_classes=frozenset(payload) if payload else None,
            )
        robot_homes = raw.get("robots", {})
        if not isinstance(robot_homes, dict):
            raise ContractError("robots 必须是 机器人->驻留区域 的对象")
        for robot_id, home in robot_homes.items():
            if home not in zones:
                raise ContractError(f"机器人 {robot_id} 的驻留区域未知: {home}")
        return cls(raw["layout_id"], zones, connectors, dict(robot_homes))

    def connectors_of(self, zone_id: str) -> list[str]:
        return list(self._by_zone.get(zone_id, ()))

    def other_end(self, connector_id: str, zone_id: str) -> str:
        connector = self.connectors[connector_id]
        first, second = connector.endpoints
        if zone_id == first:
            return second
        if zone_id == second:
            return first
        raise ContractError(f"区域 {zone_id} 不在通道 {connector_id} 上")

    def zones_along(self, origin: str, path: list[str]) -> list[str]:
        """沿通道路径依次经过的区域（含起点与终点）。"""

        sequence = [origin]
        for connector_id in path:
            sequence.append(self.other_end(connector_id, sequence[-1]))
        return sequence

    def paths(
        self,
        origin: str,
        destination: str,
        max_len: int = 6,
        max_paths: int = 8,
    ) -> list[list[str]]:
        """枚举起点到终点的简单路径（按边数、标识排序，保证确定性）。"""

        if origin not in self.zones or destination not in self.zones:
            return []
        if origin == destination:
            return [[]]
        found: list[list[str]] = []
        queue: list[tuple[str, list[str], frozenset[str]]] = [(origin, [], frozenset({origin}))]
        while queue and len(found) < max_paths:
            zone_id, path, visited = queue.pop(0)
            if len(path) >= max_len:
                continue
            for connector_id in self.connectors_of(zone_id):
                nxt = self.other_end(connector_id, zone_id)
                if nxt in visited:
                    continue
                new_path = path + [connector_id]
                if nxt == destination:
                    found.append(new_path)
                else:
                    queue.append((nxt, new_path, visited | {nxt}))
        found.sort(key=lambda p: (len(p), p))
        return found
