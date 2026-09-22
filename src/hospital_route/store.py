"""追加式 JSONL 日志：服务重启后据此完整重建状态。

日志只记录输入事实（采用的拓扑、规则版本、事件），派生状态
（任务、预留、签认）由引擎确定性重放得到，避免双写不一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

JOURNAL_NAME = "journal.jsonl"


class Journal:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / JOURNAL_NAME

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_all(self, records: Iterator[dict[str, Any]]) -> None:
        for record in records:
            self.append(record)
