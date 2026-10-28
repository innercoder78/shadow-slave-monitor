"""Shared data models."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum

class Health(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class ChapterReport:
    source: str
    chapter: int
    title: str | None
    url: str
    strategy: str = "unknown"

@dataclass(slots=True)
class RunResult:
    status: Health = Health.HEALTHY
    reasons: list[str] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)

    def degrade(self, reason: str) -> None:
        if self.status != Health.FAILED:
            self.status = Health.DEGRADED
        self.degraded_reasons.append(reason)

    def fail(self, reason: str) -> None:
        self.status = Health.FAILED
        self.reasons.append(reason)
