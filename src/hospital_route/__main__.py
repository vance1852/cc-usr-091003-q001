"""命令行入口：夜班配送调度。

示例::

    python -m hospital_route run-demo
    python -m hospital_route --config scenarios/night_isolation ingest fixtures/night_shift_part1.json
    python -m hospital_route --config scenarios/night_isolation plan
    python -m hospital_route --config scenarios/night_isolation next
    python -m hospital_route --config scenarios/night_isolation record M-0901
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .service import HospitalRouteService
from .state import parse_ts

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "scenarios" / "night_isolation"


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _service(args: argparse.Namespace) -> HospitalRouteService:
    return HospitalRouteService.load(args.config)


def _cmd_ingest(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.ingest_file(args.events)
    service.persist()
    _print(result)
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    service = _service(args)
    moment = parse_ts(args.at) if args.at else None
    if args.mission:
        result = service.plan_mission(args.mission, moment)
    else:
        result = service.plan_all(moment)
    service.persist()
    _print(result)
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    service = _service(args)
    moment = parse_ts(args.at) if args.at else None
    _print(service.next_actions(moment))
    return 0


def _cmd_blockers(args: argparse.Namespace) -> int:
    service = _service(args)
    moment = parse_ts(args.at) if args.at else None
    _print(service.blockers(moment))
    return 0


def _cmd_risk(args: argparse.Namespace) -> int:
    service = _service(args)
    _print(service.risk())
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    service = _service(args)
    _print(service.delivery_record(args.mission))
    return 0


def _cmd_takeover(args: argparse.Namespace) -> int:
    service = _service(args)
    record = service.takeover(
        operator=args.operator,
        action=args.action,
        mission_id=args.mission,
        robot_id=args.robot,
        note=args.note or "",
    )
    service.persist()
    _print({"takeover": record.__dict__})
    return 0


def _cmd_takeovers(args: argparse.Namespace) -> int:
    service = _service(args)
    _print({"takeovers": [item.__dict__ for item in service.state.takeovers]})
    return 0


def _cmd_run_demo(args: argparse.Namespace) -> int:
    """在临时目录完整重演夜班：三段事件、人工接管、重启恢复。"""

    with tempfile.TemporaryDirectory(prefix="night-shift-") as tmp:
        config = Path(tmp) / "night_isolation"
        shutil.copytree(args.config, config)
        demo_args = argparse.Namespace(**{**vars(args), "config": config})

        print("== 第一班：污染织物刚通过消毒通道，事件乱序到达 ==")
        _cmd_ingest(argparse.Namespace(**{**vars(demo_args), "events": ROOT / "fixtures" / "night_shift_part1.json"}))
        print("\n== 第一班计划：三条任务被阻断并进入人工处置 ==")
        _cmd_plan(argparse.Namespace(**{**vars(demo_args), "mission": None, "at": None}))
        print("\n== 值班视图：每台机器人下一项合法动作 ==")
        _cmd_next(argparse.Namespace(**{**vars(demo_args), "at": None}))

        print("\n== 清场与门禁回执到达（第二段事件） ==")
        _cmd_ingest(argparse.Namespace(**{**vars(demo_args), "events": ROOT / "fixtures" / "night_shift_part2.json"}))
        for mission_id in ("M-0201", "M-0301", "M-0901"):
            _cmd_takeover(
                argparse.Namespace(
                    **{
                        **vars(demo_args),
                        "operator": "duty-officer",
                        "action": "resume",
                        "mission": mission_id,
                        "robot": None,
                        "note": "清场完成，门禁已放行",
                    }
                )
            )
        print("\n== 第二班计划：电梯时段不超卖，M-0901 让行 ==")
        _cmd_plan(argparse.Namespace(**{**vars(demo_args), "mission": None, "at": None}))
        print("\n== 值班员错峰放行 M-0901 ==")
        _cmd_plan(argparse.Namespace(**{**vars(demo_args), "mission": "M-0901", "at": "2026-09-22T20:44:00+08:00"}))

        print("\n== 模拟服务重启：从持久化状态恢复 ==")
        reloaded = HospitalRouteService.load(config)
        print(f"恢复任务数 {len(reloaded.state.missions)}，预留数 {len(reloaded.state.reservations)}，"
              f"接管数 {len(reloaded.state.takeovers)}，规则版本 {reloaded.state.rule_version}")

        print("\n== 第三段事件：离线补传到达、重复回执、温控超时 ==")
        _cmd_ingest(argparse.Namespace(**{**vars(demo_args), "events": ROOT / "fixtures" / "night_shift_part3.json"}))
        print("\n== 夜班结束值班视图 ==")
        _cmd_next(argparse.Namespace(**{**vars(demo_args), "at": None}))
        print("\n== 阻断依据 ==")
        _cmd_blockers(argparse.Namespace(**{**vars(demo_args), "at": None}))
        print("\n== 污染传播 ==")
        _cmd_risk(demo_args)
        print("\n== M-0901 交付记录（门禁/清场/温控证据） ==")
        _cmd_record(argparse.Namespace(**{**vars(demo_args), "mission": "M-0901"}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hospital_route", description="院内配送机器人洁污通行调度")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="场景资料目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="接收一个事件文件（允许乱序）")
    p.add_argument("events", type=Path)
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("plan", help="为任务规划合法路线并登记预留")
    p.add_argument("--mission", help="只规划指定任务")
    p.add_argument("--at", help="规划时刻（ISO 8601，默认取最新事件时刻）")
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("next", help="每台机器人下一项合法动作")
    p.add_argument("--at", help="评估时刻（ISO 8601）")
    p.set_defaults(func=_cmd_next)

    p = sub.add_parser("blockers", help="当前阻断依据")
    p.add_argument("--at", help="评估时刻（ISO 8601）")
    p.set_defaults(func=_cmd_blockers)

    p = sub.add_parser("risk", help="污染传播视图")
    p.set_defaults(func=_cmd_risk)

    p = sub.add_parser("record", help="任务交付记录")
    p.add_argument("mission")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("takeover", help="登记人工接管")
    p.add_argument("--operator", required=True)
    p.add_argument("--action", required=True, choices=["resume", "cancel", "note"])
    p.add_argument("--mission")
    p.add_argument("--robot")
    p.add_argument("--note")
    p.set_defaults(func=_cmd_takeover)

    p = sub.add_parser("takeovers", help="查看全部人工接管记录")
    p.set_defaults(func=_cmd_takeovers)

    p = sub.add_parser("run-demo", help="在临时目录重演整个夜班")
    p.set_defaults(func=_cmd_run_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
