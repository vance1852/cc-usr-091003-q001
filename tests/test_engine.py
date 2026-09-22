"""路线合法性、时段预留不超卖与污染传播。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from hospital_route.state import parse_ts
from support import T0, evt, make_service, mission_evt

NOW = parse_ts("2026-09-22T20:00:30+08:00")


class EngineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")

    def ingest(self, *events) -> None:
        self.service.ingest(list(events))

    def plan(self, mission_id: str, at=NOW):
        return self.service.plan_mission(mission_id, at)


class RouteLegalityTest(EngineTestBase):
    def test_clean_route_commits_reservations(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "medicine", "A", "D"),
            evt("door_result", "d1", T0, door_id="door-1", edge_id="e2",
                result="granted", request_id="rq-1"),
        )
        result = self.plan("M1")
        self.assertTrue(result["planned"], result)
        # A→B→电梯→D：电梯边产生一条预留
        self.assertEqual(1, len(result["reservations"]))
        reservation = self.service.state.reservations[result["reservations"][0]]
        self.assertEqual("e3", reservation.resource_id)
        self.assertEqual("active", reservation.status)

    def test_zone_closure_blocks_route_with_evidence(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "medicine", "A", "D"),
            evt("zone_closed", "z1", T0, closure_id="cl-1", target="B", scope="zone", reason="喷雾"),
        )
        result = self.plan("M1")
        self.assertFalse(result["planned"])
        codes = [r["code"] for r in result["reasons"]]
        self.assertIn("zone_closed", codes)
        evidence = next(r for r in result["reasons"] if r["code"] == "zone_closed")["evidence"]
        self.assertEqual("cl-1", evidence["closure"]["closure_id"])

    def test_contaminated_zone_requires_valid_certificate(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "medicine", "A", "D"),
            evt("contamination_reported", "c1", T0, target="B", target_kind="zone", level="confirmed"),
        )
        blocked = self.plan("M1")
        self.assertFalse(blocked["planned"])
        self.assertIn("zone_contaminated", [r["code"] for r in blocked["reasons"]])
        self.assertTrue(blocked["suspended"])
        # 为 A、B 及被外溢的电梯补做消毒；值班员接管放行后路线恢复合法
        self.ingest(
            evt("cleaning_completed", "c2", "2026-09-22T20:00:20+08:00",
                certificate_id="cert-b", target="B", target_kind="zone",
                valid_from="2026-09-22T20:00:20+08:00", valid_until="2026-09-22T21:00:20+08:00"),
            evt("cleaning_completed", "c3", "2026-09-22T20:00:25+08:00",
                certificate_id="cert-a", target="A", target_kind="zone",
                valid_from="2026-09-22T20:00:25+08:00", valid_until="2026-09-22T21:00:25+08:00"),
            evt("cleaning_completed", "c4", "2026-09-22T20:00:26+08:00",
                certificate_id="cert-lift", target="lift-x", target_kind="elevator",
                valid_from="2026-09-22T20:00:26+08:00", valid_until="2026-09-22T21:00:26+08:00"),
        )
        self.service.takeover(operator="duty", action="resume", mission_id="M1")
        allowed = self.plan("M1")
        self.assertTrue(allowed["planned"], allowed)

    def test_expired_certificate_does_not_unblock(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "specimen", "A", "D"),
            # 电梯轿厢污染，凭证在评估时刻已过期
            evt("cleaning_completed", "c1", "2026-09-22T19:00:00+08:00",
                certificate_id="cert-1", target="lift-x", target_kind="elevator",
                valid_from="2026-09-22T19:00:00+08:00", valid_until="2026-09-22T19:30:00+08:00"),
            evt("contamination_reported", "c2", "2026-09-22T19:35:00+08:00",
                target="lift-x", target_kind="elevator", level="confirmed"),
        )
        result = self.plan("M1")
        self.assertFalse(result["planned"])
        self.assertIn("elevator_contaminated", [r["code"] for r in result["reasons"]])
        self.assertTrue(result["suspended"])
        # 重新消毒并签发有效凭证，值班员接管后放行
        self.ingest(
            evt("cleaning_completed", "c3", "2026-09-22T20:00:20+08:00",
                certificate_id="cert-2", target="lift-x", target_kind="elevator",
                valid_from="2026-09-22T20:00:20+08:00", valid_until="2026-09-22T21:00:20+08:00"),
        )
        self.service.takeover(operator="duty", action="resume", mission_id="M1")
        allowed = self.plan("M1")
        self.assertTrue(allowed["planned"], allowed)

    def test_elevator_payload_restriction(self) -> None:
        # 电梯只承运 medicine/specimen，waste 由 r2 承运但无电梯权限
        self.ingest(mission_evt("e1", "M1", "r2", "waste", "E", "A"))
        result = self.plan("M1")
        self.assertFalse(result["planned"])
        codes = [r["code"] for r in result["reasons"]]
        self.assertIn("elevator_payload", codes)
        self.assertIn("elevator_access", codes)

    def test_robot_tier_limit(self) -> None:
        # r1 max_tier=1，waste tier=2
        self.ingest(mission_evt("e1", "M1", "r1", "waste", "A", "B"))
        result = self.plan("M1")
        self.assertFalse(result["planned"])
        self.assertIn("robot_tier", [r["code"] for r in result["reasons"]])

    def test_door_pending_blocks_until_receipt(self) -> None:
        self.ingest(mission_evt("e1", "M1", "r1", "medicine", "A", "C"))
        pending = self.plan("M1")
        self.assertFalse(pending["planned"])
        self.assertIn("door_pending", [r["code"] for r in pending["reasons"]])
        self.ingest(
            evt("door_result", "d1", "2026-09-22T20:01:00+08:00", door_id="door-1",
                edge_id="e2", result="granted", request_id="rq-1"),
        )
        granted = self.plan("M1")
        self.assertTrue(granted["planned"], granted)

    def test_temp_limit_eta_beyond_deadline(self) -> None:
        # specimen 上限 500 秒；A→D 需 10+20=30 秒，可放行
        self.ingest(mission_evt("e1", "M1", "r1", "specimen", "A", "D"))
        self.assertTrue(self.plan("M1")["planned"])
        # 创建时间太早，温控时限已过
        self.ingest(
            mission_evt("e2", "M2", "r1", "specimen", "A", "D",
                        occurred="2026-09-22T19:40:00+08:00"),
        )
        late = self.plan("M2")
        self.assertFalse(late["planned"])
        self.assertIn("temp_limit", [r["code"] for r in late["reasons"]])

    def test_offline_robot_waits(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "medicine", "A", "D"),
            evt("robot_offline", "e2", T0, robot_id="r1"),
        )
        result = self.plan("M1")
        self.assertFalse(result["planned"])
        self.assertIn("robot_offline", [r["code"] for r in result["reasons"]])
        self.assertFalse(result["suspended"])


class CapacityTest(EngineTestBase):
    def test_elevator_slots_never_oversold(self) -> None:
        self.ingest(
            mission_evt("e1", "M1", "r1", "medicine", "A", "D"),
            mission_evt("e2", "M2", "r1", "specimen", "D", "A"),
        )
        first = self.plan("M1")
        self.assertTrue(first["planned"])
        # 同一时刻第二个任务挤不进容量为 1 的电梯
        second = self.plan("M2")
        self.assertFalse(second["planned"])
        self.assertIn("capacity", [r["code"] for r in second["reasons"]])
        # 错峰后放行
        later = self.plan("M2", parse_ts("2026-09-22T20:02:00+08:00"))
        self.assertTrue(later["planned"], later)
        # 任意时刻占用不超过容量 1
        active = [r for r in self.service.state.reservations.values()
                  if r.resource_id == "e3" and r.status == "active"]
        self.assertEqual(2, len(active))
        self.assertFalse(
            parse_ts(active[0].start) < parse_ts(active[1].end)
            and parse_ts(active[1].start) < parse_ts(active[0].end)
        )

    def test_narrow_corridor_directional_capacity(self) -> None:
        self.ingest(
            evt("door_result", "d1", T0, door_id="door-1", edge_id="e2",
                result="granted", request_id="rq-1"),
            mission_evt("e1", "M1", "r1", "medicine", "A", "C"),
            mission_evt("e2", "M2", "r1", "medicine", "A", "C"),
        )
        self.assertTrue(self.plan("M1")["planned"])
        # 同向第二个任务在相同时段被容量拦截
        blocked = self.plan("M2")
        self.assertFalse(blocked["planned"])
        self.assertIn("capacity", [r["code"] for r in blocked["reasons"]])

    def test_replan_replaces_stale_reservations(self) -> None:
        self.ingest(mission_evt("e1", "M1", "r1", "medicine", "A", "D"))
        first = self.plan("M1")
        self.assertTrue(first["planned"])
        again = self.plan("M1", parse_ts("2026-09-22T20:05:00+08:00"))
        self.assertTrue(again["planned"])
        active = [r for r in self.service.state.reservations.values()
                  if r.mission_id == "M1" and r.status == "active"]
        self.assertEqual(1, len(active))
        # 电梯段在走廊段（10 秒）之后开始
        self.assertEqual("2026-09-22T20:05:10+08:00", active[0].start)


class TransitContaminationTest(EngineTestBase):
    def test_waste_transit_contaminates_and_spreads(self) -> None:
        self.ingest(mission_evt("e1", "M1", "r2", "waste", "E", "D"))
        result = self.plan("M1")
        self.assertTrue(result["planned"], result)
        state = self.service.state
        # 废物途经 D：D 升为 confirmed，并外溢到邻区
        self.assertEqual("confirmed", state.zone_contamination["D"])
        self.assertEqual("suspected", state.elevator_contamination["lift-x"])

    def test_sealed_payload_does_not_contaminate(self) -> None:
        self.ingest(mission_evt("e1", "M1", "r1", "specimen", "A", "D"))
        result = self.plan("M1")
        self.assertTrue(result["planned"], result)
        state = self.service.state
        self.assertEqual("clean", state.zone_contamination["B"])
        self.assertEqual("clean", state.zone_contamination["D"])
        self.assertEqual("clean", state.elevator_contamination["lift-x"])


if __name__ == "__main__":
    unittest.main()
