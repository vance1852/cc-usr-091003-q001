"""夜班场景端到端：乱序事件、人工接管、重启恢复与交付追溯。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hospital_route.service import HospitalRouteService
from hospital_route.state import parse_ts

SCENARIO = ROOT / "scenarios" / "night_isolation"
FIXTURES = ROOT / "fixtures"


class NightShiftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "night_isolation"
        shutil.copytree(SCENARIO, self.config)
        self.service = HospitalRouteService.load(self.config)

    def _reload(self) -> HospitalRouteService:
        return HospitalRouteService.load(self.config)

    def test_first_watch_blocks_contaminated_routes(self) -> None:
        result = self.service.ingest_file(FIXTURES / "night_shift_part1.json")
        self.assertEqual(9, result["applied"])
        self.assertEqual([], result["rejections"])
        plan = self.service.plan_all()
        by_mission = {item["mission_id"]: item for item in plan["results"]}
        # 一层内配送放行
        self.assertTrue(by_mission["M-0401"]["planned"])
        # 标本任务：消毒通道污染且无凭证 → 人工处置
        self.assertFalse(by_mission["M-0201"]["planned"])
        self.assertTrue(by_mission["M-0201"]["suspended"])
        self.assertIn("zone_contaminated", [r["code"] for r in by_mission["M-0201"]["reasons"]])
        # 污染物任务：门禁拒绝 → 人工处置
        self.assertFalse(by_mission["M-0301"]["planned"])
        self.assertIn("door_denied", [r["code"] for r in by_mission["M-0301"]["reasons"]])
        # 药品任务在门禁回执事件到达时即被暂停
        self.assertEqual("suspended", self.service.state.missions["M-0901"].status)
        # 同一门禁请求重送的回执只登记不推进
        duplicates = [a for a in self.service.state.audit if a.kind == "duplicate_door_result"]
        self.assertEqual(1, len(duplicates))
        # 污染沿新消毒通道外溢到关联区域
        risk = self.service.risk()
        self.assertEqual("confirmed", risk["contaminated_zones"]["decon-channel"])
        self.assertEqual("suspected", risk["contaminated_zones"]["lab-clean"])
        self.assertEqual("suspected", risk["contaminated_zones"]["iso-nurse"])

    def _ingest_part1_and_plan(self) -> None:
        self.service.ingest_file(FIXTURES / "night_shift_part1.json")
        self.service.plan_all()

    def _ingest_part2_and_resume(self) -> None:
        self.service.ingest_file(FIXTURES / "night_shift_part2.json")
        for mission_id in ("M-0201", "M-0301", "M-0901"):
            self.service.takeover(
                operator="duty-officer", action="resume", mission_id=mission_id,
                note="清场完成，门禁已放行",
            )

    def test_second_watch_releases_after_cleaning(self) -> None:
        self._ingest_part1_and_plan()
        self._ingest_part2_and_resume()
        plan = self.service.plan_all()
        by_mission = {item["mission_id"]: item for item in plan["results"]}
        self.assertTrue(by_mission["M-0201"]["planned"], by_mission["M-0201"])
        self.assertTrue(by_mission["M-0301"]["planned"], by_mission["M-0301"])
        # 电梯容量为 1：M-0901 与 M-0201 时段冲突，等待错峰
        self.assertFalse(by_mission["M-0901"]["planned"])
        self.assertIn("capacity", [r["code"] for r in by_mission["M-0901"]["reasons"]])
        # 值班员错峰放行
        later = self.service.plan_mission("M-0901", parse_ts("2026-09-22T20:44:00+08:00"))
        self.assertTrue(later["planned"], later)
        self.assertEqual(2, len(later["reservations"]))
        # 电梯任意时刻占用不超过容量 1
        lift_windows = [
            (r.start, r.end)
            for r in self.service.state.reservations.values()
            if r.resource_id == "e-lift-a" and r.status == "active"
        ]
        for i, (s1, e1) in enumerate(lift_windows):
            for s2, e2 in lift_windows[i + 1:]:
                self.assertFalse(
                    parse_ts(s1) < parse_ts(e2) and parse_ts(s2) < parse_ts(e1),
                    "电梯时段被超卖",
                )
        # 污染织物转运把污染带到途经区域并外溢
        risk = self.service.risk()
        self.assertEqual("confirmed", risk["contaminated_zones"]["iso-entry"])
        self.assertEqual("suspected", risk["contaminated_elevators"]["lift-a"])
        # 每次人工接管都被记录
        self.assertEqual(3, len(self.service.state.takeovers))

    def test_restart_recovers_and_late_events_do_not_rewrite(self) -> None:
        self._ingest_part1_and_plan()
        self._ingest_part2_and_resume()
        self.service.plan_all()
        self.service.plan_mission("M-0901", parse_ts("2026-09-22T20:44:00+08:00"))
        self.service.persist()

        reloaded = self._reload()
        self.assertEqual(len(self.service.state.missions), len(reloaded.state.missions))
        self.assertEqual(len(self.service.state.reservations), len(reloaded.state.reservations))
        self.assertEqual(3, len(reloaded.state.takeovers))
        self.assertEqual("night-2026.09-v3", reloaded.state.rule_version)

        reloaded.ingest_file(FIXTURES / "night_shift_part3.json")
        missions = reloaded.state.missions
        # 补传到达：温控未超 → 签认送达
        self.assertEqual("delivered", missions["M-0901"].status)
        # 已签认任务不被后续补传倒改
        late = [a for a in reloaded.state.audit if a.kind == "late_event_ignored"]
        self.assertTrue(any("M-0901" in entry.summary for entry in late))
        self.assertEqual("delivered", missions["M-0901"].status)
        # 温控超时：不得签认，进入人工处置暂停
        self.assertEqual("suspended", missions["M-0902"].status)
        self.assertEqual("temp_limit_exceeded", missions["M-0902"].hold_reasons[-1]["code"])

        # 值班视图：其余任务均已交付，只剩温控超时任务等待人工处置
        actions = reloaded.next_actions()
        by_robot = {item["robot_id"]: item for item in actions["robots"]}
        self.assertEqual("await_manual", by_robot["medbot-07"]["action"])
        self.assertEqual("idle", by_robot["specbot-03"]["action"])
        self.assertEqual("idle", by_robot["washbot-02"]["action"])
        blockers = reloaded.blockers()
        blocked_ids = {item["mission_id"] for item in blockers["blocked"]}
        self.assertIn("M-0902", blocked_ids)

        # 交付记录可追溯门禁、清场与温控证据
        record = reloaded.delivery_record("M-0901")
        self.assertEqual("delivered", record["status"])
        self.assertEqual("night-2026.09-v3", record["rule_version"])
        self.assertEqual(2, len(record["reservations"]))
        self.assertTrue(record["cleaning_certificates"])
        door_events = [a for a in reloaded.state.audit if a.kind == "door_result"]
        self.assertTrue(any(e.data.get("edge_id") == "e-channel-nurse" for e in door_events))
        # 重启后交付记录一致
        reloaded.persist()
        again = self._reload()
        self.assertEqual(record, again.delivery_record("M-0901"))

    def test_full_night_is_deterministic(self) -> None:
        def run() -> dict:
            service = HospitalRouteService.load(self.config)
            service.ingest_file(FIXTURES / "night_shift_part1.json")
            service.plan_all()
            service.ingest_file(FIXTURES / "night_shift_part2.json")
            for mission_id in ("M-0201", "M-0301", "M-0901"):
                service.takeover(operator="duty-officer", action="resume", mission_id=mission_id)
            service.plan_all()
            service.plan_mission("M-0901", parse_ts("2026-09-22T20:44:00+08:00"))
            service.ingest_file(FIXTURES / "night_shift_part3.json")
            return {
                "missions": {k: m.status for k, m in sorted(service.state.missions.items())},
                "zones": dict(sorted(service.state.zone_contamination.items())),
                "audit_kinds": [a.kind for a in service.state.audit],
            }

        first = run()
        shutil.rmtree(self.config)
        shutil.copytree(SCENARIO, self.config)
        second = run()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
