"""系列诊断只驻留内存；凭据脱敏与游标分页集中实现。"""

import copy
import re
from collections import deque
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from .core.models import PLUGIN_ID, PLUGIN_NAME, SERIES_ID

SECRET_KEY = re.compile(
    r"token|api[_-]?key|secret|password|authorization|cookie|jwt|private[_-]?key|ssh[_-]?key|provider[_-]?key|bridge[_-]?key",
    re.I,
)
SECRET_ASSIGNMENT = re.compile(
    r"((?:[\w.-]*(?:token|api[_-]?key|secret|password|authorization|jwt|private[_-]?key|ssh[_-]?key|provider[_-]?key|bridge[_-]?key)[\w.-]*)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s&;,}\]]+)",
    re.I,
)
HIDDEN = "<已隐藏>"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): HIDDEN if SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            HIDDEN,
            value,
            flags=re.S,
        )
        value = re.sub(
            r"(?im)(?:set-cookie|cookie)\s*[:=]\s*[^\r\n]+", "Cookie: " + HIDDEN, value
        )
        value = re.sub(
            r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "Bearer " + HIDDEN, value
        )
        value = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", HIDDEN, value
        )
        return SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + HIDDEN, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class DiagnosticBuffer:
    def __init__(self):
        self._events: deque[dict] = deque(maxlen=1000)
        self._seq = 0
        self._stream_id = uuid4().hex
        self._lock = RLock()

    def emit(
        self, level: str, code: str, summary: str, details: dict | None = None
    ) -> None:
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Invalid diagnostic level")
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "plugin_id": PLUGIN_ID,
                    "plugin_name": PLUGIN_NAME,
                    "level": level,
                    "code": redact(str(code))[:80],
                    "summary": redact(str(summary))[:1000],
                    "details": redact(copy.deepcopy(details or {})),
                }
            )

    def contract(self) -> dict:
        return {
            "name": "series.diagnostics",
            "version": "1.0",
            "series_id": SERIES_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_name": PLUGIN_NAME,
            "capabilities": ["read_events", "clear"],
            "storage": "memory_only",
            "astrbot_log_propagation": False,
            "secrets_in_response": False,
        }

    def events(self, after_seq: int = 0, limit: int = 200) -> dict:
        if (
            type(after_seq) is not int
            or after_seq < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            return {
                "events": [],
                "next_seq": 0,
                "dropped_before": 0,
                "stream_id": self._stream_id,
                "code": "INVALID_CURSOR",
            }
        with self._lock:
            rows = [event for event in self._events if event["seq"] > after_seq][:limit]
            return {
                "events": copy.deepcopy(rows),
                "next_seq": rows[-1]["seq"] if rows else after_seq,
                "dropped_before": self._events[0]["seq"] - 1 if self._events else 0,
                "stream_id": self._stream_id,
            }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._seq = 0
            self._stream_id = uuid4().hex
