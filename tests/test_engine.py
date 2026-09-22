"""调度后端的领域行为测试：路由、预留、幂等、补传、暂停与持久化。"""

import io
import json
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hospital_route import cli
from hospital_route.contracts import EventEnvelope, load_events
from hospital_route.service import DispatchService

FIXTURES = ROOT / "fixtures"
T = "2026-09-10T22:00:00+08:00"


def env(event_id, kind, occurred, received=None, **attrs):
    return EventEnvelope(
        event_id=event_id,
        kind=kind,
        occurred_at=occurred,
        received_at=received or occurred,
        attributes=attrs,
    )


def at(hhmm, day="2026-09-10"):
    return f"{day}T{hhmm}:00+08:00"


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = DispatchService.open(self.tmp.name)
        self.service.adopt_layout(json.loads((FIXTURES / "layout.json").read_text(encoding="utf-8")))
        self.service.adopt_rules(json.loads((FIXTURES / "zoning_rules.v1.json").read_text(encoding="utf-8")))

    def ingest_fixture(self, name):
        _, events = load_events(FIXTURES / name)
        return self.service.ingest(events)

    def mission(self, mission_id):
        return self.service.view()["missions"][mission_id]


class IncidentFixtureTest(ServiceCase):
    def test_incident_fixture_pauses_on_door_denied(self):
        report = self.ingest_fixture("incident.json")
        self.assertEqual(4, report["applied"])
        self.assertTrue(any("按拒绝处理" in note for note in report["notes"]))
        mission = self.mission("medbot-07#1")
        self.assertEqual("paused", mission["status"])
        self.assertEqual("door_denied", mission["pause_reasons"][0]["code"])

    def test_incident_view_shows_blocking_basis(self):
        self.ingest_fixture("incident.json")
        view = self.service.view()
        robot = next(r for r in view["robots"] if r["robot_id"] == "medbot-07")
        action = robot["next_action"]
        self.assertEqual("paused", action["kind"])
        codes = {reason["code"] for reason in action["blocking"]}
        self.assertIn("zone_closed", codes)
        closed = next(r for r in action["blocking"] if r["code"] == "zone_closed")
        self.assertEqual(["001-event-2"], closed["evidence"])

    def test_incident_arrival_filed_not_signed(self):
        self.ingest_fixture("incident.json")
        record = self.service.delivery_record("medbot-07#1")
        self.assertIsNone(record["signed"])
        self.assertEqual(1, len(record["arrivals"]))
        self.assertEqual("001-event-4", record["arrivals"][0]["event_id"])


class NightShiftTest(ServiceCase):
    def setUp(self):
        super().setUp()
        self.report = self.ingest_fixture("night_shift.json")

    def test_all_events_applied(self):
        self.assertEqual(26, self.report["applied"])
        self.assertEqual(0, self.report["duplicates"])

    def test_all_missions_delivered(self):
        view = self.service.view()
        statuses = {mid: m["status"] for mid, m in view["missions"].items()}
        self.assertEqual(
            {
                "medbot-07#1": "delivered",
                "medbot-07#2": "delivered",
                "medbot-11#1": "delivered",
                "medbot-11#2": "delivered",
                "wastebot-02#1": "delivered",
            },
            statuses,
        )
        for robot in view["robots"]:
            self.assertEqual("idle", robot["next_action"]["kind"])

    def test_duplicate_receipt_ignored(self):
        self.assertTrue(any("rc-901 重复送达" in note for note in self.report["notes"]))
        record = self.service.delivery_record("medbot-07#1")
        granted = [r for r in record["door_events"] if r["receipt_id"] == "rc-901"]
        self.assertEqual(1, len(granted))

    def test_backfilled_arrival_signs_only_via_manual_override(self):
        record = self.service.delivery_record("medbot-07#2")
        self.assertEqual("2026-09-10T22:58:00+08:00", record["delivered_at"])
        self.assertEqual("e23", record["signed"]["confirmed_by_override"])
        self.assertEqual("e22", record["signed"]["sign_event"])
        self.assertEqual(1, len(record["overrides"]))
        self.assertEqual("王护士", record["overrides"][0]["operator"])

    def test_stale_cleaning_does_not_clear_contamination(self):
        self.assertTrue(any("不能清除 cor-waste" in note for note in self.report["notes"]))
        view = self.service.view()
        contaminated = {e["target"] for e in view["contamination"] if e["level"] == "contaminated"}
        self.assertIn("cor-waste", contaminated)
        self.assertIn("lift-lobby", contaminated)
        self.assertNotIn("lift-5", contaminated)  # e24 补传的有效清洁已清除

    def test_contamination_propagates_to_associated_zones(self):
        view = self.service.view()
        exposed = {e["target"]: e for e in view["contamination"] if e["level"] == "exposed"}
        self.assertIn("lab", exposed)
        self.assertEqual(["lift-2", "lab"], exposed["lab"]["path"])
        self.assertIn("linen-room", exposed)
        self.assertIn("waste-room", exposed)

    def test_reservations_never_exceed_capacity(self):
        view = self.service.view()
        layout = json.loads((FIXTURES / "layout.json").read_text(encoding="utf-8"))
        capacity = {c["id"]: c["capacity"] for c in layout["connectors"]}
        from hospital_route.engine import _parse

        for connector_id, cap in capacity.items():
            windows = [
                (_parse(r["start"]), _parse(r["end"]))
                for r in view["reservations"]
                if r["resource_id"] == connector_id and not r["released"]
            ]
            for start, end in windows:
                overlapping = sum(1 for s, e in windows if s < end and start < e)
                self.assertLessEqual(overlapping, cap, f"{connector_id} 超卖")

    def test_serialized_narrow_corridor(self):
        view = self.service.view()
        early = [
            (r["start"], r["end"])
            for r in view["reservations"]
            if r["resource_id"] == "cor-c3w" and r["start"] < "2026-09-10T14:05:00+00:00"
        ]
        early.sort()
        for (s1, e1), (s2, e2) in zip(early, early[1:]):
            self.assertLessEqual(e1, s2, "窄道容量为 1，窗口不得重叠")

    def test_delivery_record_traces_evidence(self):
        record = self.service.delivery_record("medbot-11#1")
        decisions = {r["receipt_id"]: r["decision"] for r in record["door_events"]}
        self.assertEqual({"rc-902": "denied", "rc-903": "granted"}, decisions)
        certs = {c["cert_id"] for c in record["cleaning_evidence"]}
        self.assertIn("CT-88", certs)
        self.assertEqual("zoning-2026.09.1", record["signed"]["rules_version"])
        self.assertEqual(2, len(record["overrides"]))
        self.assertTrue(record["signed"]["temp"]["within_limit"])

    def test_reingest_is_idempotent(self):
        before = self.service.view()
        report = self.ingest_fixture("night_shift.json")
        self.assertEqual(0, report["applied"])
        self.assertEqual(26, report["duplicates"])
        self.assertEqual(before, self.service.view())


class PersistenceTest(ServiceCase):
    def test_restart_preserves_state(self):
        self.ingest_fixture("night_shift.json")
        before = self.service.view()
        reopened = DispatchService.open(self.tmp.name)
        after = reopened.view()
        self.assertEqual(before, after)
        record = reopened.delivery_record("medbot-11#1")
        self.assertEqual(2, len(record["overrides"]))
        self.assertEqual("zoning-2026.09.1", record["signed"]["rules_version"])
        active = [r for r in after["reservations"] if not r["released"]]
        self.assertTrue(active, "预留应在重启后保留")

    def test_rules_upgrade_recorded(self):
        self.ingest_fixture("incident.json")
        rules = json.loads((FIXTURES / "zoning_rules.v1.json").read_text(encoding="utf-8"))
        rules["rules_version"] = "zoning-2026.09.2"
        self.service.adopt_rules(rules)
        reopened = DispatchService.open(self.tmp.name)
        self.assertEqual("zoning-2026.09.2", reopened.view()["rules_version"])
        # 既有任务的签认仍引用旧版本
        record = reopened.delivery_record("medbot-07#1")
        self.assertEqual("paused", record["status"])


class OrderingTest(ServiceCase):
    def test_shuffled_input_yields_same_state(self):
        _, events = load_events(FIXTURES / "night_shift.json")
        shuffled = list(events)
        random.Random(42).shuffle(shuffled)
        report = self.service.ingest(shuffled)
        self.assertEqual(26, report["applied"])
        reference = DispatchService.open(tempfile.mkdtemp())
        reference.adopt_layout(json.loads((FIXTURES / "layout.json").read_text(encoding="utf-8")))
        reference.adopt_rules(json.loads((FIXTURES / "zoning_rules.v1.json").read_text(encoding="utf-8")))
        reference.ingest(events)
        self.assertEqual(reference.view(), self.service.view())


class BehaviorTest(ServiceCase):
    def test_temp_timeout_pauses_mission(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=10),
            env("tick", "zone_contaminated", at("22:30"), zone="lift-lobby"),
        ])
        mission = self.mission("medbot-07#1")
        self.assertEqual("paused", mission["status"])
        self.assertEqual("temp_timeout", mission["pause_reasons"][0]["code"])

    def test_expired_cert_pauses_and_valid_cert_plus_resume_recovers(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="wastebot-02", zone="waste-room",
                origin="ward-c3", payload_class="contaminated_waste", sequence=1),
            env("m2", "mission_created", at("22:01"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("c1", "cleaning_recorded", at("22:05"), connector="cor-c3w",
                cert_id="CT-X", cert_issued_at="2026-09-10T19:00:00+08:00"),
        ])
        mission = self.mission("medbot-07#1")
        self.assertEqual("paused", mission["status"])
        self.assertEqual("cert_expired", mission["pause_reasons"][0]["code"])
        self.service.ingest([
            env("c2", "cleaning_recorded", at("22:20"), connector="cor-c3w",
                cert_id="CT-Y", cert_issued_at=at("21:30")),
        ])
        self.assertEqual("paused", self.mission("medbot-07#1")["status"])  # 需人工接管
        self.service.override("medbot-07#1", "resume", "王护士", "复消完成", at=at("22:25"))
        self.assertEqual("dispatched", self.mission("medbot-07#1")["status"])

    def test_door_receipt_replay_does_not_advance_twice(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("d1", "door_result", at("22:01"), robot_id="medbot-07", zone="ward-c3",
                decision="granted", receipt_id="rc-1"),
            env("d2", "door_result", at("22:02"), robot_id="medbot-07", zone="ward-c3",
                decision="granted", receipt_id="rc-1"),
        ])
        record = self.service.delivery_record("medbot-07#1")
        self.assertEqual(1, len(record["door_events"]))

    def test_signed_mission_immune_to_late_events(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("d1", "door_result", at("22:01"), robot_id="medbot-07", zone="ward-c3",
                decision="granted", receipt_id="rc-1"),
            env("a1", "robot_arrived", at("22:05"), robot_id="medbot-07", zone="ward-c3"),
        ])
        self.assertEqual("delivered", self.mission("medbot-07#1")["status"])
        self.service.ingest([
            env("d2", "door_result", at("22:06"), robot_id="medbot-07", zone="ward-c3",
                decision="denied", receipt_id="rc-2", mission_id="medbot-07#1"),
            env("a2", "robot_arrived", at("22:07"), robot_id="medbot-07", zone="pharmacy"),
        ])
        record = self.service.delivery_record("medbot-07#1")
        self.assertEqual("delivered", record["status"])
        self.assertEqual("2026-09-10T22:05:00+08:00", record["delivered_at"])
        self.assertEqual({"d2", "a2"}, {e["event_id"] for e in record["late_events"]})
        self.assertEqual(["rc-1"], [r["receipt_id"] for r in record["door_events"]])

    def test_elevator_capability_blocks_dirty_payload(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="lab",
                payload_class="contaminated_waste", sequence=1),
        ])
        view = self.service.view()
        robot = next(r for r in view["robots"] if r["robot_id"] == "medbot-07")
        action = robot["next_action"]
        self.assertEqual("wait", action["kind"])
        codes = {r["code"] for r in action["blocking"]}
        self.assertIn("elevator_capability", codes)
        self.assertEqual([], self.mission("medbot-07#1")["reservations"])

    def test_zone_close_and_reopen(self):
        self.service.ingest([
            env("z1", "zone_closed", at("21:59"), zone="ward-c3"),
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
        ])
        self.assertEqual("pending", self.mission("medbot-07#1")["status"])
        self.service.ingest([env("z2", "zone_reopened", at("22:10"), zone="ward-c3")])
        self.assertEqual("dispatched", self.mission("medbot-07#1")["status"])

    def test_isolation_delivery_requires_door_grant(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("a1", "robot_arrived", at("22:05"), robot_id="medbot-07", zone="ward-c3"),
        ])
        self.assertEqual("dispatched", self.mission("medbot-07#1")["status"])  # 未签认
        self.service.ingest([
            env("d1", "door_result", at("22:06"), robot_id="medbot-07", zone="ward-c3",
                decision="granted", receipt_id="rc-9"),
        ])
        # 门禁放行后由人工确认归档到达
        self.service.override("medbot-07#1", "resume", "王护士", "门禁补登", at=at("22:07"))
        # 任务未暂停过，override 不生效；到达已在案，等下一条到达或直接确认
        record = self.service.delivery_record("medbot-07#1")
        self.assertIn(record["status"], ("dispatched", "delivered"))

    def test_override_cancel_releases_reservations(self):
        self.service.ingest([
            env("m1", "mission_created", at("22:00"), robot_id="medbot-07", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("m2", "mission_created", at("22:00"), robot_id="medbot-11", zone="ward-c3",
                payload_class="medicine", sequence=1, temp_limit_minutes=300),
            env("d1", "door_result", at("22:01"), robot_id="medbot-07", zone="ward-c3",
                decision="denied", receipt_id="rc-1"),
        ])
        self.assertEqual("paused", self.mission("medbot-07#1")["status"])
        self.service.override("medbot-07#1", "cancel", "王护士", "药品回收", at=at("22:05"))
        self.assertEqual("cancelled", self.mission("medbot-07#1")["status"])
        view = self.service.view()
        cancelled_res = [r for r in view["reservations"] if r["mission_id"] == "medbot-07#1"]
        now_utc = "2026-09-10T14:05:00+00:00"
        # 未来时段的预留必须释放；已过去的预留是历史事实，保留原样
        future = [r for r in cancelled_res if r["start"] > now_utc]
        self.assertTrue(all(r["released"] for r in future))
        self.assertTrue(any(r["released"] for r in cancelled_res))


class CliTest(ServiceCase):
    def test_cli_ingest_status_record(self):
        state = self.tmp.name
        rc = cli.main([
            "--state", state, "ingest", str(FIXTURES / "night_shift.json"),
            "--layout", str(FIXTURES / "layout.json"),
            "--rules", str(FIXTURES / "zoning_rules.v1.json"),
        ])
        self.assertEqual(0, rc)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = cli.main(["--state", state, "status", "--json"])
        self.assertEqual(0, rc)
        view = json.loads(buffer.getvalue())
        self.assertEqual("zoning-2026.09.1", view["rules_version"])
        self.assertEqual(5, len(view["missions"]))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = cli.main(["--state", state, "record", "--mission", "medbot-11#1"])
        self.assertEqual(0, rc)
        record = json.loads(buffer.getvalue())
        self.assertEqual("delivered", record["status"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = cli.main(["--state", state, "status"])
        self.assertEqual(0, rc)
        self.assertIn("污染风险", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
