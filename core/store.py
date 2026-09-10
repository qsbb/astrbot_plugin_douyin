"""小规模操作日志：先落盘再提交，重启后的在途操作一律按结果未知处理。"""

import asyncio
import copy
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import PluginError


class StateStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "runtime.json"
        self._lock = asyncio.Lock()
        self.state = {"schema_version": 1, "paused": False, "actions": {}}
        if self.path.exists():
            try:
                if self.path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("State exceeds limit")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(raw, dict)
                    or raw.get("schema_version") != 1
                    or type(raw.get("paused")) is not bool
                    or not isinstance(raw.get("actions"), dict)
                    or len(raw["actions"]) > 2048
                ):
                    raise ValueError("Invalid state schema")
                for key, record in raw["actions"].items():
                    if (
                        not isinstance(record, dict)
                        or not isinstance(key, str)
                        or not isinstance(record.get("fingerprint"), str)
                        or not isinstance(record.get("receipt"), dict)
                    ):
                        raise ValueError("Invalid action record")
                    timestamp = datetime.fromisoformat(record["created_at"])
                    if timestamp.tzinfo is None:
                        raise ValueError("Timestamp must include timezone")
                    if record["receipt"].get("status") == "pending":
                        record["receipt"].update(
                            status="unknown_result",
                            code="INTERRUPTED_AFTER_RESERVATION",
                            message="上次执行在完成前中断，必须人工核对，不能重发。",
                        )
                self.state = raw
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise PluginError(
                    "STATE_INVALID", "运行状态文件损坏或版本不受支持，请先备份并检查。"
                ) from exc

    async def update(self, mutation: Callable[[dict], None]) -> None:
        async with self._lock:
            candidate = copy.deepcopy(self.state)
            mutation(candidate)
            # 仅淘汰超过一周且已有明确结果的记录；未知结果保留，避免重启重发。
            cutoff = datetime.now(UTC) - timedelta(days=7)
            candidate["actions"] = {
                key: record
                for key, record in candidate["actions"].items()
                if record["receipt"].get("status") in ("pending", "unknown_result")
                or datetime.fromisoformat(record["created_at"]) >= cutoff
            }
            if len(candidate["actions"]) > 2048:
                raise PluginError("JOURNAL_FULL", "操作日志已满，请先核对未完成记录。")
            writer = asyncio.create_task(asyncio.to_thread(self._write, candidate))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                # to_thread 不能中止磁盘写入；先同步内存状态，避免取消后下一次写覆盖它。
                await writer
                self.state = candidate
                raise
            self.state = candidate

    def _write(self, candidate: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(
            prefix="runtime-", suffix=".json", dir=self.path.parent
        )
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
