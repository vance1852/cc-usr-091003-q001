"""静态资料模型：楼层拓扑、洁污分区规则、电梯能力与机器人档案。

资料以 JSON 字典载入，未知字段一律拒绝，避免现场配置笔误被静默忽略。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import ContractError

PAYLOAD_CLASSES = ("medicine", "specimen", "linen_clean", "waste")
ZONE_KINDS = ("clean", "buffer", "dirty")
EDGE_KINDS = ("corridor", "narrow", "elevator")
EDGE_KIND_RANK = {"corridor": 0, "narrow": 1, "elevator": 2}
DIRECTIONS = ("forward", "reverse")


class ModelError(ContractError):
    """静态资料不符合建模约定。"""


def _require_str(raw: Mapping[str, Any], key: str, ctx: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{ctx} 缺少字符串字段 {key}")
    return value


def _require_number(raw: Mapping[str, Any], key: str, ctx: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelError(f"{ctx} 缺少数值字段 {key}")
    return float(value)


def _require_int(raw: Mapping[str, Any], key: str, ctx: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{ctx} 缺少整数字段 {key}")
    return value


def _require_str_list(raw: Mapping[str, Any], key: str, ctx: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ModelError(f"{ctx} 缺少非空数组字段 {key}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ModelError(f"{ctx} 字段 {key} 含有非法元素")
        items.append(item)
    return tuple(items)


def _reject_unknown(raw: Mapping[str, Any], allowed: Iterable[str], ctx: str) -> None:
    extra = sorted(set(raw) - set(allowed))
    if extra:
        raise ModelError(f"{ctx} 含未识别字段：{'、'.join(extra)}")


@dataclass(frozen=True)
class Zone:
    zone_id: str
    name: str
    kind: str
    floor: str
    adjacency: tuple[tuple[str, ...], ...]
    elevator_banks: tuple[str, ...]

    @property
    def is_dirty(self) -> bool:
        return self.kind == "dirty"


@dataclass(frozen=True)
class Edge:
    edge_id: str
    a: str
    b: str
    kind: str
    base_cost: float
    capacity: int
    elevator_id: str | None
    door_id: str | None = None
    door_required: bool = False

    def other(self, zone_id: str) -> str:
        if zone_id == self.a:
            return self.b
        if zone_id == self.b:
            return self.a
        raise ModelError(f"区域 {zone_id} 不在边 {self.edge_id} 上")


@dataclass(frozen=True)
class Elevator:
    elevator_id: str
    edge_id: str
    serves: tuple[str, ...]
    capacity: int
    allowed_payloads: tuple[str, ...]
    cycle_cost: float


@dataclass(frozen=True)
class Topology:
    zones: dict[str, Zone]
    edges: dict[str, Edge]
    elevators: dict[str, Elevator]

    def zone(self, zone_id: str) -> Zone:
        try:
            return self.zones[zone_id]
        except KeyError:
            raise ModelError(f"未知区域 {zone_id}") from None

    def edge(self, edge_id: str) -> Edge:
        try:
            return self.edges[edge_id]
        except KeyError:
            raise ModelError(f"未知边 {edge_id}") from None

    def zone_elevator_ids(self, zone_id: str) -> tuple[str, ...]:
        return self.zone(zone_id).elevator_banks


@dataclass(frozen=True)
class RuleSet:
    rule_version: str
    payload_classes: tuple[str, ...]
    payload_tiers: dict[str, int]
    contamination_tiers: dict[str, int]
    contamination_spread: dict[str, tuple[str, ...]]
    cleaning_valid_seconds: int
    temp_limits: dict[str, int]
    requires_certificate: tuple[str, ...]
    elevator_clean_min_tier: int

    def tier_of(self, payload_class: str) -> int:
        try:
            return self.payload_tiers[payload_class]
        except KeyError:
            raise ModelError(f"未知载荷类别 {payload_class}") from None


@dataclass(frozen=True)
class RobotProfile:
    robot_id: str
    home_zone: str
    max_tier: int
    elevator_access: tuple[str, ...]
    payload_classes: tuple[str, ...]


def load_topology(raw: Mapping[str, Any]) -> Topology:
    if not isinstance(raw, Mapping):
        raise ModelError("拓扑必须是对象")
    _reject_unknown(raw, ("zones", "edges", "elevators"), "拓扑")
    zones_raw = raw.get("zones")
    edges_raw = raw.get("edges")
    elevators_raw = raw.get("elevators", [])
    if not isinstance(zones_raw, list) or not zones_raw:
        raise ModelError("拓扑缺少 zones 数组")
    if not isinstance(edges_raw, list) or not edges_raw:
        raise ModelError("拓扑缺少 edges 数组")
    if not isinstance(elevators_raw, list):
        raise ModelError("拓扑 elevators 必须是数组")

    zones: dict[str, Zone] = {}
    for item in zones_raw:
        ctx = f"区域[{item.get('zone_id', '?') if isinstance(item, Mapping) else '?'}]"
        if not isinstance(item, Mapping):
            raise ModelError("zones 元素必须是对象")
        _reject_unknown(item, ("zone_id", "name", "kind", "floor", "adjacency", "elevator_banks"), ctx)
        zone_id = _require_str(item, "zone_id", ctx)
        kind = _require_str(item, "kind", ctx)
        if kind not in ZONE_KINDS:
            raise ModelError(f"{ctx} kind 非法：{kind}")
        adjacency_raw = item.get("adjacency")
        if not isinstance(adjacency_raw, list) or not adjacency_raw:
            raise ModelError(f"{ctx} 缺少 adjacency 数组")
        adjacency: list[tuple[str, ...]] = []
        for group in adjacency_raw:
            if not isinstance(group, list) or not group:
                raise ModelError(f"{ctx} adjacency 分组必须是非空数组")
            parsed_group: list[str] = []
            for entry in group:
                if not isinstance(entry, str) or not entry.strip():
                    raise ModelError(f"{ctx} adjacency 分组含非法元素")
                parsed_group.append(entry)
            adjacency.append(tuple(parsed_group))
        elevator_banks = item.get("elevator_banks", [])
        if not isinstance(elevator_banks, list) or any(
            not isinstance(bank, str) or not bank.strip() for bank in elevator_banks
        ):
            raise ModelError(f"{ctx} elevator_banks 必须是字符串数组")
        zone = Zone(
            zone_id=zone_id,
            name=_require_str(item, "name", ctx),
            kind=kind,
            floor=_require_str(item, "floor", ctx),
            adjacency=tuple(adjacency),
            elevator_banks=tuple(elevator_banks),
        )
        if zone.zone_id in zones:
            raise ModelError(f"区域重复：{zone.zone_id}")
        zones[zone.zone_id] = zone

    edges: dict[str, Edge] = {}
    for item in edges_raw:
        ctx = f"边[{item.get('edge_id', '?') if isinstance(item, Mapping) else '?'}]"
        if not isinstance(item, Mapping):
            raise ModelError("edges 元素必须是对象")
        _reject_unknown(item, ("edge_id", "a", "b", "kind", "base_cost", "capacity", "elevator_id", "door_id", "door_required"), ctx)
        edge_id = _require_str(item, "edge_id", ctx)
        kind = _require_str(item, "kind", ctx)
        if kind not in EDGE_KINDS:
            raise ModelError(f"{ctx} kind 非法：{kind}")
        a = _require_str(item, "a", ctx)
        b = _require_str(item, "b", ctx)
        if a == b:
            raise ModelError(f"{ctx} 两端区域相同")
        if a not in zones or b not in zones:
            raise ModelError(f"{ctx} 引用了未知区域")
        capacity = _require_int(item, "capacity", ctx)
        if capacity < 1:
            raise ModelError(f"{ctx} capacity 必须为正整数")
        base_cost = _require_number(item, "base_cost", ctx)
        if base_cost <= 0:
            raise ModelError(f"{ctx} base_cost 必须为正数")
        elevator_id = item.get("elevator_id")
        if elevator_id is not None and (not isinstance(elevator_id, str) or not elevator_id.strip()):
            raise ModelError(f"{ctx} elevator_id 非法")
        if kind == "elevator" and elevator_id is None:
            raise ModelError(f"{ctx} 电梯边必须声明 elevator_id")
        if kind != "elevator" and elevator_id is not None:
            raise ModelError(f"{ctx} 非电梯边不得声明 elevator_id")
        door_id = item.get("door_id")
        if door_id is not None and (not isinstance(door_id, str) or not door_id.strip()):
            raise ModelError(f"{ctx} door_id 非法")
        door_required_raw = item.get("door_required", door_id is not None)
        if not isinstance(door_required_raw, bool):
            raise ModelError(f"{ctx} door_required 必须是布尔值")
        door_required = door_required_raw
        if door_required and door_id is None:
            raise ModelError(f"{ctx} 声明 door_required 必须同时给出 door_id")
        edge = Edge(
            edge_id=edge_id,
            a=a,
            b=b,
            kind=kind,
            base_cost=base_cost,
            capacity=capacity,
            elevator_id=elevator_id,
            door_required=door_required,
        )
        if edge.edge_id in edges:
            raise ModelError(f"边重复：{edge.edge_id}")
        edges[edge.edge_id] = edge

    elevators: dict[str, Elevator] = {}
    for item in elevators_raw:
        ctx = f"电梯[{item.get('elevator_id', '?') if isinstance(item, Mapping) else '?'}]"
        if not isinstance(item, Mapping):
            raise ModelError("elevators 元素必须是对象")
        _reject_unknown(item, ("elevator_id", "edge_id", "serves", "capacity", "allowed_payloads", "cycle_cost"), ctx)
        elevator_id = _require_str(item, "elevator_id", ctx)
        edge_id = _require_str(item, "edge_id", ctx)
        if edge_id not in edges:
            raise ModelError(f"{ctx} 引用了未知边 {edge_id}")
        edge = edges[edge_id]
        if edge.kind != "elevator":
            raise ModelError(f"{ctx} 引用的边 {edge_id} 不是电梯边")
        if edge.elevator_id != elevator_id:
            raise ModelError(f"{ctx} 与边 {edge_id} 的 elevator_id 不一致")
        serves = _require_str_list(item, "serves", ctx)
        for zone_id in serves:
            if zone_id not in zones:
                raise ModelError(f"{ctx} 停靠未知区域 {zone_id}")
        capacity = _require_int(item, "capacity", ctx)
        if capacity < 1:
            raise ModelError(f"{ctx} capacity 必须为正整数")
        allowed_payloads = _require_str_list(item, "allowed_payloads", ctx)
        cycle_cost = _require_number(item, "cycle_cost", ctx)
        if cycle_cost <= 0:
            raise ModelError(f"{ctx} cycle_cost 必须为正数")
        elevator = Elevator(
            elevator_id=elevator_id,
            edge_id=edge_id,
            serves=serves,
            capacity=capacity,
            allowed_payloads=allowed_payloads,
            cycle_cost=cycle_cost,
        )
        if elevator.elevator_id in elevators:
            raise ModelError(f"电梯重复：{elevator.elevator_id}")
        elevators[elevator.elevator_id] = elevator

    topology = Topology(zones=zones, edges=edges, elevators=elevators)
    _validate_topology(topology)
    return topology


def _validate_topology(topology: Topology) -> None:
    for edge in topology.edges.values():
        if edge.kind == "elevator":
            assert edge.elevator_id is not None
            if edge.elevator_id not in topology.elevators:
                raise ModelError(f"边 {edge.edge_id} 引用了未知电梯 {edge.elevator_id}")
    for elevator in topology.elevators.values():
        edge = topology.edges[elevator.edge_id]
        endpoints = (edge.a, edge.b)
        for zone_id in elevator.serves:
            if zone_id not in endpoints:
                raise ModelError(
                    f"电梯 {elevator.elevator_id} 停靠 {zone_id}，但轿厢边仅连接 {'/'.join(endpoints)}"
                )
        for zone_id in endpoints:
            if zone_id not in elevator.serves:
                raise ModelError(
                    f"电梯 {elevator.elevator_id} 未声明停靠端点 {zone_id}"
                )
    for zone in topology.zones.values():
        for bank in zone.elevator_banks:
            if bank not in topology.elevators:
                raise ModelError(f"区域 {zone.zone_id} 引用了未知电梯 {bank}")
            if zone.zone_id not in topology.elevators[bank].serves:
                raise ModelError(f"区域 {zone.zone_id} 不在电梯 {bank} 的停靠列表中")
    # 邻接矩阵引用的边必须存在且确实连接该区域
    for zone in topology.zones.values():
        for group in zone.adjacency:
            for edge_id in group:
                edge = topology.edge(edge_id)
                if zone.zone_id not in (edge.a, edge.b):
                    raise ModelError(f"区域 {zone.zone_id} 的邻接边 {edge_id} 不连接该区域")


def load_rules(raw: Mapping[str, Any]) -> RuleSet:
    if not isinstance(raw, Mapping):
        raise ModelError("规则必须是对象")
    _reject_unknown(
        raw,
        (
            "rule_version",
            "payload_classes",
            "payload_tiers",
            "contamination_tiers",
            "contamination_spread",
            "cleaning_valid_seconds",
            "temp_limits",
            "requires_certificate",
            "elevator_clean_min_tier",
        ),
        "规则",
    )
    rule_version = _require_str(raw, "rule_version", "规则")
    payload_classes = _require_str_list(raw, "payload_classes", "规则")
    if len(set(payload_classes)) != len(payload_classes):
        raise ModelError("规则 payload_classes 重复")

    payload_tiers_raw = raw.get("payload_tiers")
    if not isinstance(payload_tiers_raw, Mapping):
        raise ModelError("规则缺少 payload_tiers 对象")
    payload_tiers: dict[str, int] = {}
    for name in payload_classes:
        value = payload_tiers_raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelError(f"规则 payload_tiers.{name} 必须是非负整数")
        payload_tiers[name] = value
    extra = sorted(set(payload_tiers_raw) - set(payload_classes))
    if extra:
        raise ModelError(f"规则 payload_tiers 含未知载荷：{'、'.join(extra)}")

    contamination_tiers_raw = raw.get("contamination_tiers")
    if not isinstance(contamination_tiers_raw, Mapping) or not contamination_tiers_raw:
        raise ModelError("规则缺少 contamination_tiers 对象")
    contamination_tiers: dict[str, int] = {}
    for key, value in contamination_tiers_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ModelError("规则 contamination_tiers 含非法键")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelError(f"规则 contamination_tiers.{key} 必须是非负整数")
        contamination_tiers[key] = value

    spread_raw = raw.get("contamination_spread")
    if not isinstance(spread_raw, Mapping) or not spread_raw:
        raise ModelError("规则缺少 contamination_spread 对象")
    contamination_spread: dict[str, tuple[str, ...]] = {}
    for key, value in spread_raw.items():
        if key not in contamination_tiers:
            raise ModelError(f"规则 contamination_spread 引用了未知档位 {key}")
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ModelError(f"规则 contamination_spread.{key} 必须是字符串数组")
        for target in value:
            if target not in contamination_tiers:
                raise ModelError(f"规则 contamination_spread.{key} 指向未知档位 {target}")
        contamination_spread[key] = tuple(value)

    cleaning_valid_seconds = raw.get("cleaning_valid_seconds")
    if isinstance(cleaning_valid_seconds, bool) or not isinstance(cleaning_valid_seconds, int) or cleaning_valid_seconds <= 0:
        raise ModelError("规则 cleaning_valid_seconds 必须是正整数")

    temp_limits_raw = raw.get("temp_limits")
    if not isinstance(temp_limits_raw, Mapping):
        raise ModelError("规则缺少 temp_limits 对象")
    temp_limits: dict[str, int] = {}
    for name in payload_classes:
        value = temp_limits_raw.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ModelError(f"规则 temp_limits.{name} 必须是正整数或 null")
        temp_limits[name] = value
    extra = sorted(set(temp_limits_raw) - set(payload_classes))
    if extra:
        raise ModelError(f"规则 temp_limits 含未知载荷：{'、'.join(extra)}")

    requires_certificate_raw = raw.get("requires_certificate", [])
    if not isinstance(requires_certificate_raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in requires_certificate_raw
    ):
        raise ModelError("规则 requires_certificate 必须是字符串数组")
    for name in requires_certificate_raw:
        if name not in payload_classes:
            raise ModelError(f"规则 requires_certificate 含未知载荷 {name}")

    elevator_clean_min_tier = raw.get("elevator_clean_min_tier")
    if isinstance(elevator_clean_min_tier, bool) or not isinstance(elevator_clean_min_tier, int) or elevator_clean_min_tier < 0:
        raise ModelError("规则 elevator_clean_min_tier 必须是非负整数")

    return RuleSet(
        rule_version=rule_version,
        payload_classes=tuple(payload_classes),
        payload_tiers=payload_tiers,
        contamination_tiers=contamination_tiers,
        contamination_spread=contamination_spread,
        cleaning_valid_seconds=cleaning_valid_seconds,
        temp_limits=temp_limits,
        requires_certificate=tuple(requires_certificate_raw),
        elevator_clean_min_tier=elevator_clean_min_tier,
    )


def load_robots(raw: Any) -> dict[str, RobotProfile]:
    if not isinstance(raw, list) or not raw:
        raise ModelError("机器人档案必须是非空数组")
    robots: dict[str, RobotProfile] = {}
    for item in raw:
        ctx = f"机器人[{item.get('robot_id', '?') if isinstance(item, Mapping) else '?'}]"
        if not isinstance(item, Mapping):
            raise ModelError("机器人档案元素必须是对象")
        _reject_unknown(item, ("robot_id", "home_zone", "max_tier", "elevator_access", "payload_classes"), ctx)
        robot_id = _require_str(item, "robot_id", ctx)
        max_tier = _require_int(item, "max_tier", ctx)
        if max_tier < 0:
            raise ModelError(f"{ctx} max_tier 必须是非负整数")
        elevator_access = item.get("elevator_access", [])
        if not isinstance(elevator_access, list) or any(
            not isinstance(entry, str) or not entry.strip() for entry in elevator_access
        ):
            raise ModelError(f"{ctx} elevator_access 必须是字符串数组")
        payload_classes = _require_str_list(item, "payload_classes", ctx)
        profile = RobotProfile(
            robot_id=robot_id,
            home_zone=_require_str(item, "home_zone", ctx),
            max_tier=max_tier,
            elevator_access=tuple(elevator_access),
            payload_classes=payload_classes,
        )
        if robot_id in robots:
            raise ModelError(f"机器人重复：{robot_id}")
        robots[robot_id] = profile
    return robots


def validate_robots_against(
    robots: Mapping[str, RobotProfile], topology: Topology, rules: RuleSet
) -> None:
    for profile in robots.values():
        topology.zone(profile.home_zone)
        for elevator_id in profile.elevator_access:
            if elevator_id not in topology.elevators:
                raise ModelError(f"机器人 {profile.robot_id} 引用了未知电梯 {elevator_id}")
        for name in profile.payload_classes:
            if name not in rules.payload_classes:
                raise ModelError(f"机器人 {profile.robot_id} 声明了未知载荷 {name}")
