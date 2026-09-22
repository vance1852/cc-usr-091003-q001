"""事件处理：确定性重放、幂等、终态保护与污染传播。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import T0, evt, make_service, mission_evt


class EventOrderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _service(self, name: str = "svc"):
        return make_service(Path(self.tmp.name) / name)

    def test_out_of_order_events_replay_deterministically(self) -> None:
        events = [
            mission_evt("e1", "M1", "r1", "medicine", "A", "B", occurred="2026-09-22T20:00:00+08:00",
                        received="2026-09-22T20:00:05+08:00"),
            evt("robot_offline", "e2", "2026-09-22T20:01:00+08:00", "2026-09-22T20:01:05+08:00", robot_id="r1"),
            evt("robot_online", "e3", "2026-09-22T20:02:00+08:00", "2026-09-22T20:02:05+08:00",
                robot_id="r1", zone="A"),
        ]
        first = self._service("a")
        first.ingest(events)
        second = self._service("b")
        second.ingest(list(reversed(events)))
        self.assertEqual(
            first.state.robots["r1"].online, second.state.robots["r1"].online
        )
        self.assertTrue(first.state.robots["r1"].online)
        self.assertEqual(
            [m.status for m in first.state.missions.values()],
            [m.status for m in second.state.missions.values()],
        )

    def test_duplicate_event_id_applied_once(self) -> None:
        service = self._service()
        event = mission_evt("e1", "M1", "r1", "medicine", "A", "B")
        service.ingest([event, event])
        self.assertEqual(1, len(service.state.missions))
        duplicates = [a for a in service.state.audit if a.kind == "duplicate_event"]
        self.assertEqual(1, len(duplicates))

    def test_unknown_robot_rejected_without_state_change(self) -> None:
        service = self._service()
        service.ingest([mission_evt("e1", "M1", "ghost", "medicine", "A", "B")])
        self.assertEqual(0, len(service.state.missions))
        self.assertEqual("unknown_robot", service.state.rejections[0].code)


class DoorReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")

    def test_same_request_receipt_cannot_advance_twice(self) -> None:
        first = evt("door_result", "d1", T0, door_id="door-1", edge_id="e2",
                    result="denied", request_id="rq-1")
        resend = evt("door_result", "d2", "2026-09-22T20:01:00+08:00",
                     door_id="door-1", edge_id="e2", result="granted", request_id="rq-1")
        self.service.ingest([first, resend])
        record = self.service.state.doors["rq-1"]
        self.assertEqual("denied", record.result)
        self.assertEqual("d1", record.event_id)
        duplicates = [a for a in self.service.state.audit if a.kind == "duplicate_door_result"]
        self.assertEqual(1, len(duplicates))

    def test_new_request_supersedes_old_receipt(self) -> None:
        denied = evt("door_result", "d1", T0, door_id="door-1", edge_id="e2",
                     result="denied", request_id="rq-1")
        granted = evt("door_result", "d2", "2026-09-22T20:05:00+08:00",
                      door_id="door-1", edge_id="e2", result="granted", request_id="rq-2")
        self.service.ingest([denied, granted])
        door = self.service.engine._door_of(self.service.state, "e2")
        self.assertIsNotNone(door)
        self.assertEqual("granted", door.result)

    def test_door_denied_suspends_linked_mission(self) -> None:
        self.service.ingest([
            mission_evt("e1", "M1", "r1", "medicine", "A", "C"),
            evt("door_result", "d1", "2026-09-22T20:01:00+08:00", door_id="door-1",
                edge_id="e2", result="denied", request_id="rq-1", mission_id="M1"),
        ])
        mission = self.service.state.missions["M1"]
        self.assertEqual("suspended", mission.status)
        self.assertEqual("door_denied", mission.hold_reasons[-1]["code"])


class FinalStateProtectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")
        self.service.ingest([
            mission_evt("e1", "M1", "r2", "waste", "C", "E",
                        occurred="2026-09-22T20:00:00+08:00"),
            evt("robot_arrived", "e2", "2026-09-22T20:10:00+08:00",
                robot_id="r2", zone="E", mission_id="M1"),
        ])
        self.assertEqual("delivered", self.service.state.missions["M1"].status)

    def test_late_arrival_does_not_rewrite_signed_mission(self) -> None:
        before = self.service.state.missions["M1"].finalized_at
        self.service.ingest([
            evt("robot_arrived", "e3", "2026-09-22T20:11:00+08:00",
                "2026-09-22T20:30:00+08:00", robot_id="r2", zone="C", mission_id="M1"),
        ])
        mission = self.service.state.missions["M1"]
        self.assertEqual("delivered", mission.status)
        self.assertEqual(before, mission.finalized_at)
        late = [a for a in self.service.state.audit if a.kind == "late_event_ignored"]
        self.assertEqual(1, len(late))

    def test_late_cancel_does_not_rewrite_signed_mission(self) -> None:
        self.service.ingest([
            evt("mission_cancelled", "e4", "2026-09-22T20:12:00+08:00",
                "2026-09-22T20:31:00+08:00", mission_id="M1"),
        ])
        self.assertEqual("delivered", self.service.state.missions["M1"].status)

    def test_late_cleaning_event_does_not_rewrite_confirmed_certificate(self) -> None:
        self.service.ingest([
            evt("cleaning_completed", "c1", "2026-09-22T20:20:00+08:00",
                certificate_id="cert-1", target="B", target_kind="zone",
                valid_from="2026-09-22T20:20:00+08:00", valid_until="2026-09-22T21:20:00+08:00"),
            evt("cleaning_completed", "c2", "2026-09-22T20:21:00+08:00",
                "2026-09-22T20:40:00+08:00",
                certificate_id="cert-1", target="B", target_kind="zone",
                valid_from="2026-09-22T20:21:00+08:00", valid_until="2026-09-22T21:00:00+08:00"),
        ])
        cert = self.service.state.certificates["cert-1"]
        self.assertEqual("2026-09-22T21:20:00+08:00", cert.valid_until)
        late = [a for a in self.service.state.audit if a.kind == "late_event_ignored"]
        self.assertTrue(any("cert-1" in entry.summary for entry in late))


class TempLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")

    def test_temp_breach_at_signoff_goes_to_manual_hold(self) -> None:
        # specimen 上限 500 秒；到达距创建 600 秒
        self.service.ingest([
            mission_evt("e1", "M1", "r1", "specimen", "A", "B",
                        occurred="2026-09-22T20:00:00+08:00"),
            evt("robot_arrived", "e2", "2026-09-22T20:10:00+08:00",
                robot_id="r1", zone="B", mission_id="M1"),
        ])
        mission = self.service.state.missions["M1"]
        self.assertEqual("suspended", mission.status)
        self.assertEqual("temp_limit_exceeded", mission.hold_reasons[-1]["code"])
        self.assertIn("temp_breach", mission.evidence)


class ContaminationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")

    def test_confirmed_zone_spreads_to_adjacent_zones_and_elevator(self) -> None:
        self.service.ingest([
            evt("contamination_reported", "c1", T0, target="B", target_kind="zone", level="confirmed"),
        ])
        state = self.service.state
        self.assertEqual("confirmed", state.zone_contamination["B"])
        # B 的邻接：e1→A（走廊）、e2→C（窄道）、e3→lift-x（电梯）
        self.assertEqual("suspected", state.zone_contamination["A"])
        self.assertEqual("suspected", state.zone_contamination["C"])
        self.assertEqual("suspected", state.elevator_contamination["lift-x"])
        propagation = [a for a in state.audit if a.kind == "contamination_propagated"]
        self.assertEqual(1, len(propagation))
        targets = {(t["target_kind"], t["target_id"]) for t in propagation[0].data["targets"]}
        self.assertIn(("elevator", "lift-x"), targets)

    def test_cleaning_resets_zone_to_clean(self) -> None:
        self.service.ingest([
            evt("contamination_reported", "c1", T0, target="B", target_kind="zone", level="confirmed"),
            evt("cleaning_completed", "c2", "2026-09-22T20:30:00+08:00",
                certificate_id="cert-1", target="B", target_kind="zone",
                valid_from="2026-09-22T20:30:00+08:00", valid_until="2026-09-22T22:30:00+08:00"),
        ])
        self.assertEqual("clean", self.service.state.zone_contamination["B"])
        # 已外溢的邻区污染不因源头消毒而消失
        self.assertEqual("suspected", self.service.state.zone_contamination["A"])


class ClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = make_service(Path(self.tmp.name) / "svc")

    def test_closure_register_and_lift(self) -> None:
        self.service.ingest([
            evt("zone_closed", "z1", T0, closure_id="cl-1", target="B", scope="zone", reason="喷雾消毒"),
        ])
        self.assertEqual("active", self.service.state.closures["cl-1"].status)
        self.service.ingest([
            evt("zone_reopened", "z2", "2026-09-22T21:00:00+08:00", closure_id="cl-1"),
        ])
        self.assertEqual("lifted", self.service.state.closures["cl-1"].status)

    def test_late_reopen_after_lift_ignored(self) -> None:
        self.service.ingest([
            evt("zone_closed", "z1", T0, closure_id="cl-1", target="B", scope="zone"),
            evt("zone_reopened", "z2", "2026-09-22T21:00:00+08:00", closure_id="cl-1"),
            evt("zone_reopened", "z3", "2026-09-22T21:05:00+08:00",
                "2026-09-22T21:30:00+08:00", closure_id="cl-1"),
        ])
        closure = self.service.state.closures["cl-1"]
        self.assertEqual("lifted", closure.status)
        self.assertEqual("2026-09-22T21:00:00+08:00", closure.lifted_at)


if __name__ == "__main__":
    unittest.main()
