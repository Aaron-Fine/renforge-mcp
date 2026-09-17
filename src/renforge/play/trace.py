from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .contracts import SCHEMA_VERSION
from .profiles import _private_directory, _write_json


class TraceJournal:
    """Single-writer durable JSONL journal with recoverable tail semantics."""

    def __init__(self, root: Path, trace_id: str, metadata: dict[str, Any]):
        self.root = root / "traces" / trace_id
        _private_directory(self.root)
        for name in ("observations", "frames", "checkpoints"):
            _private_directory(self.root / name)
        self.events = self.root / "events.jsonl"
        self._lock = threading.Lock()
        self._truncate_invalid_tail()
        self._sequence = self._last_sequence()
        trace = {"schema_version": SCHEMA_VERSION, "trace_id": trace_id, "status": "active", **metadata}
        if not (self.root / "trace.json").exists():
            _write_json(self.root / "trace.json", trace)

    def _last_sequence(self) -> int:
        valid = self.read_events(self.events)
        return int(valid[-1]["sequence"]) if valid else 0

    def _truncate_invalid_tail(self) -> None:
        if not self.events.exists():
            return
        offset = 0
        sequence = 0
        with self.events.open("rb") as stream:
            while True:
                raw = stream.readline()
                if not raw:
                    offset = stream.tell()
                    break
                if not raw.endswith(b"\n"):
                    break
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if not isinstance(value, dict) or value.get("sequence") != sequence + 1:
                    break
                sequence += 1
                offset = stream.tell()
        if self.events.stat().st_size != offset:
            with self.events.open("r+b") as stream:
                stream.truncate(offset)
                stream.flush()
                os.fsync(stream.fileno())

    @staticmethod
    def read_events(path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            return result
        with stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if not isinstance(value, dict) or value.get("sequence") != len(result) + 1:
                    break
                result.append(value)
        return result

    def append(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            value = {"schema_version": SCHEMA_VERSION, "sequence": self._sequence, "event": event}
            if payload:
                value.update(payload)
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            fd = os.open(self.events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            return value

    def finish(self, status: str, reason: str) -> None:
        metadata_path = self.root / "trace.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("status") != "active":
            raise ValueError("Trace is already finished")
        self.append("trace.finished", {"status": status, "reason": reason})
        metadata.update(status=status, reason=reason)
        _write_json(metadata_path, metadata)
