from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Final

SCHEMA_VERSION: Final = 1
SUPPORTED_RENPY_VERSION: Final = "8.2.0.24012702"


class Lifecycle(StrEnum):
    PREPARED = "prepared"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


class FinishStatus(StrEnum):
    COMPLETE = "complete"
    NEEDS_INTERVENTION = "needs_intervention"
    FAILED = "failed"


@dataclass(frozen=True)
class Identity:
    project_id: str
    profile_id: str
    session_id: str
    trace_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    project_root: str
    project_id: str
    launcher: str | None
    engine_version: str | None
    source_file_count: int
    backend: str
    refusals: tuple[str, ...]
    source_completeness: str = "unverified"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["refusals"] = list(self.refusals)
        return value
