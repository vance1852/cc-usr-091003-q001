"""静态资料（拓扑/规则/机器人）的严格校验。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from hospital_route.model import ModelError, load_robots, load_rules, load_topology, validate_robots_against
from support import ROBOTS, RULES, TOPOLOGY


class TopologyModelTest(unittest.TestCase):
    def test_valid_topology_loads(self) -> None:
        topo = load_topology(copy.deepcopy(TOPOLOGY))
        self.assertEqual(5, len(topo.zones))
        self.assertEqual("lift-x", topo.edges["e3"].elevator_id)
        self.assertTrue(topo.edges["e2"].door_required)

    def test_unknown_field_rejected(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["zones"][0]["unexpected"] = 1
        with self.assertRaises(ModelError):
            load_topology(raw)

    def test_unknown_zone_reference_rejected(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["edges"][0]["b"] = "NOWHERE"
        with self.assertRaises(ModelError):
            load_topology(raw)

    def test_elevator_edge_requires_elevator_id(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["edges"][2]["elevator_id"] = None
        with self.assertRaises(ModelError):
            load_topology(raw)

    def test_elevator_must_serve_both_endpoints(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["elevators"][0]["serves"] = ["B"]
        with self.assertRaises(ModelError):
            load_topology(raw)

    def test_adjacency_must_reference_connecting_edge(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["zones"][0]["adjacency"] = [["e4"]]
        with self.assertRaises(ModelError):
            load_topology(raw)

    def test_door_required_needs_door_id(self) -> None:
        raw = copy.deepcopy(TOPOLOGY)
        raw["edges"][1] = {k: v for k, v in raw["edges"][1].items() if k != "door_id"}
        raw["edges"][1]["door_required"] = True
        with self.assertRaises(ModelError):
            load_topology(raw)


class RuleModelTest(unittest.TestCase):
    def test_valid_rules_load(self) -> None:
        rules = load_rules(copy.deepcopy(RULES))
        self.assertEqual("test-v1", rules.rule_version)
        self.assertEqual(2, rules.tier_of("waste"))

    def test_unknown_payload_in_tiers_rejected(self) -> None:
        raw = copy.deepcopy(RULES)
        raw["payload_tiers"]["ghost"] = 9
        with self.assertRaises(ModelError):
            load_rules(raw)

    def test_spread_must_reference_known_levels(self) -> None:
        raw = copy.deepcopy(RULES)
        raw["contamination_spread"]["confirmed"] = ["ghost"]
        with self.assertRaises(ModelError):
            load_rules(raw)

    def test_temp_limit_must_be_positive_or_null(self) -> None:
        raw = copy.deepcopy(RULES)
        raw["temp_limits"]["medicine"] = -5
        with self.assertRaises(ModelError):
            load_rules(raw)


class RobotModelTest(unittest.TestCase):
    def test_robot_references_validated(self) -> None:
        topo = load_topology(copy.deepcopy(TOPOLOGY))
        rules = load_rules(copy.deepcopy(RULES))
        robots = load_robots(copy.deepcopy(ROBOTS))
        validate_robots_against(robots, topo, rules)
        bad = copy.deepcopy(ROBOTS)
        bad[0]["elevator_access"] = ["lift-ghost"]
        robots = load_robots(bad)
        with self.assertRaises(ModelError):
            validate_robots_against(robots, topo, rules)


if __name__ == "__main__":
    unittest.main()
