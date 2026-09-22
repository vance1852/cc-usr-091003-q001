"""带版本的洁污分区规则。

规则版本随每次调度决策落账，便于事后追溯"当时按哪版规则放行"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import ContractError

CLEANLINESS = ("clean", "sealed", "dirty")


@dataclass(frozen=True)
class Rules:
    version: str
    temp_limits_minutes: dict[str, int]
    cert_valid_minutes: int
    payload_cleanliness: dict[str, str]
    zone_grade_access: dict[str, frozenset[str]]
    connector_grade_access: dict[str, frozenset[str]]
    slot_horizon_minutes: int
    slot_step_seconds: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rules":
        if not isinstance(raw, dict) or not isinstance(raw.get("rules_version"), str):
            raise ContractError("规则文件缺少 rules_version")
        cleanliness = raw.get("payload_cleanliness")
        if not isinstance(cleanliness, dict) or not cleanliness:
            raise ContractError("规则文件缺少 payload_cleanliness")
        for payload_class, level in cleanliness.items():
            if level not in CLEANLINESS:
                raise ContractError(f"载荷 {payload_class} 的洁污级别非法: {level}")

        def _grade_table(name: str) -> dict[str, frozenset[str]]:
            table = raw.get(name)
            if not isinstance(table, dict):
                raise ContractError(f"规则文件缺少 {name}")
            missing = [level for level in CLEANLINESS if level not in table]
            if missing:
                raise ContractError(f"{name} 缺少级别: {'、'.join(missing)}")
            return {level: frozenset(table[level]) for level in CLEANLINESS}

        temp_limits = raw.get("temp_limits_minutes", {})
        if not isinstance(temp_limits, dict):
            raise ContractError("temp_limits_minutes 必须是对象")
        return cls(
            version=raw["rules_version"],
            temp_limits_minutes={key: int(val) for key, val in temp_limits.items()},
            cert_valid_minutes=int(raw.get("cert_valid_minutes", 120)),
            payload_cleanliness=dict(cleanliness),
            zone_grade_access=_grade_table("zone_grade_access"),
            connector_grade_access=_grade_table("connector_grade_access"),
            slot_horizon_minutes=int(raw.get("slot_horizon_minutes", 120)),
            slot_step_seconds=int(raw.get("slot_step_seconds", 30)),
        )

    def cleanliness_of(self, payload_class: str) -> str:
        level = self.payload_cleanliness.get(payload_class)
        if level is None:
            raise ContractError(f"未知载荷类型: {payload_class}")
        return level

    def temp_limit_for(self, payload_class: str) -> int | None:
        return self.temp_limits_minutes.get(payload_class)
