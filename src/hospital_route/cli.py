"""命令行入口：事件接入、值班视图、人工接管与交付记录查询。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import ContractError, load_events
from .service import DispatchService


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def _open_service(args: argparse.Namespace) -> DispatchService:
    service = DispatchService.open(args.state)
    layout_path = getattr(args, "layout", None)
    rules_path = getattr(args, "rules", None)
    if layout_path:
        layout_id = service.adopt_layout(_load_json(layout_path))
        print(f"已采用拓扑 {layout_id}", file=sys.stderr)
    if rules_path:
        version = service.adopt_rules(_load_json(rules_path))
        print(f"已采用规则版本 {version}", file=sys.stderr)
    return service


def cmd_ingest(args: argparse.Namespace) -> int:
    service = _open_service(args)
    scenario, events = load_events(args.events)
    report = service.ingest(events)
    print(f"场景 {scenario}: 应用 {report['applied']} 条，忽略重复 {report['duplicates']} 条")
    for note in report["notes"]:
        print(f"  - {note}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    service = _open_service(args)
    view = service.view(at=args.at)
    if args.json:
        _print_json(view)
        return 0
    _render_text(view)
    return 0


def _render_text(view: dict[str, Any]) -> None:
    print(f"规则版本: {view['rules_version']}  拓扑: {view['layout_id']}  时间: {view['generated_at']}")
    print("== 机器人 ==")
    for robot in view["robots"]:
        print(f"[{robot['robot_id']}] 位置 {robot['location']}")
        action = robot["next_action"]
        print(f"  下一动作: {action['kind']} — {action.get('detail', '')}")
        for reason in action.get("pause_reasons", []):
            print(f"    暂停原因 {reason['code']}: {reason['detail']} (证据 {','.join(reason['evidence'])})")
        for reason in action.get("blocking", []):
            print(f"    阻断 {reason['code']}: {reason['detail']}")
        for reservation in action.get("reservations", []):
            print(
                f"    预留 {reservation['reservation_id']} {reservation['resource_id']} "
                f"{reservation['start']} -> {reservation['end']}"
            )
        for item in robot["queue"]:
            print(f"    队列 {item['mission_id']} [{item['status']}]")
    print("== 污染风险 ==")
    if not view["contamination"]:
        print("  无")
    for entry in view["contamination"]:
        source = entry.get("source", {})
        origin = source.get("mission_id") or source.get("event_id") or "外部"
        print(
            f"  [{entry['level']}] {entry['target']} 自 {entry['since']} "
            f"来源 {origin} 路径 {'>'.join(entry['path'])}"
        )
    print("== 预留 ==")
    active = [r for r in view["reservations"] if not r["released"]]
    if not active:
        print("  无")
    for reservation in active:
        print(
            f"  {reservation['reservation_id']} {reservation['resource_id']} "
            f"{reservation['start']} -> {reservation['end']} 任务 {reservation['mission_id']}"
        )


def cmd_record(args: argparse.Namespace) -> int:
    service = _open_service(args)
    try:
        record = service.delivery_record(args.mission)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_json(record)
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    service = _open_service(args)
    report = service.override(
        mission_id=args.mission,
        action=args.action,
        operator=args.operator,
        reason=args.reason,
        at=args.at,
    )
    for note in report["notes"]:
        print(f"  - {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hospital_route",
        description="院内配送机器人洁污通行调度后端",
    )
    parser.add_argument("--state", default="var/state", help="状态目录（默认 var/state）")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--layout", help="拓扑 JSON（首次或更换拓扑时提供）")
        command.add_argument("--rules", help="规则 JSON（首次或升级规则版本时提供）")

    ingest = sub.add_parser("ingest", help="接入一批事件")
    ingest.add_argument("events", help="场景事件 JSON")
    common(ingest)
    ingest.set_defaults(func=cmd_ingest)

    status = sub.add_parser("status", help="值班视图")
    status.add_argument("--at", help="视图参考时间（ISO8601，默认取最新事件时间）")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    common(status)
    status.set_defaults(func=cmd_status)

    record = sub.add_parser("record", help="任务交付记录（含门禁/清场/温控证据）")
    record.add_argument("--mission", required=True, help="任务标识")
    common(record)
    record.set_defaults(func=cmd_record)

    override = sub.add_parser("override", help="人工接管暂停任务")
    override.add_argument("--mission", required=True)
    override.add_argument("--action", choices=["resume", "cancel"], required=True)
    override.add_argument("--operator", required=True)
    override.add_argument("--reason", required=True)
    override.add_argument("--at", help="操作时间（ISO8601，默认取最新事件时间+1s）")
    common(override)
    override.set_defaults(func=cmd_override)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ContractError as exc:
        print(f"契约错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
