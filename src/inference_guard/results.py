"""External run reports, deliberately separate from the demo benchmark v2 schema."""

import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from inference_guard.measurement import percentile
from inference_guard.scenario_models import ScenarioEvidence
from inference_guard.validation import StrictModel


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class Attempt(StrictModel):
    index: int
    case: str
    started_ms: float
    duration_ms: float = 0
    status_code: int | None = None
    backend: str | None = None
    error: (
        Literal["http_status", "validation", "timeout", "transport", "cancelled", "execution"]
        | None
    ) = None
    error_type: str | None = None
    reason: str | None = None
    check_index: int | None = None


class Phase(StrictModel):
    elapsed_seconds: float = 0
    requests: int = 0
    successful: int = 0
    failed: int = 0
    requests_per_second: float = 0
    latency_ms: dict[str, float | None] = Field(default_factory=dict)
    errors: dict[str, int] = Field(default_factory=dict)
    samples: list[Attempt] = Field(default_factory=list)

    def summarize(self) -> None:
        self.samples.sort(key=lambda sample: sample.index)
        self.requests = len(self.samples)
        self.failed = sum(sample.error is not None for sample in self.samples)
        self.successful = self.requests - self.failed
        self.requests_per_second = (
            self.requests / self.elapsed_seconds if self.elapsed_seconds else 0
        )
        self.errors = dict(Counter(sample.error for sample in self.samples if sample.error))
        durations = [sample.duration_ms for sample in self.samples]
        self.latency_ms = {
            f"p{p}": percentile(durations, p) if durations else None for p in (50, 95, 99)
        }


class AssertionResult(StrictModel):
    name: str
    passed: bool
    actual: float | None
    limit: float


class RunReport(StrictModel):
    report_type: Literal["bront.run"] = "bront.run"
    schema_version: Literal[2] = 2
    started_at: str = Field(default_factory=timestamp)
    finished_at: str | None = None
    status: Literal["running", "passed", "failed", "error", "cancelled"] = "running"
    reason: str | None = None
    target: dict[str, JsonValue]
    workload: dict[str, JsonValue]
    cases_sha256: str
    warmup: Phase = Field(default_factory=Phase)
    measurement: Phase = Field(default_factory=Phase)
    assertions: list[AssertionResult] = Field(default_factory=list)
    scenario: ScenarioEvidence | None = None


def write_report(report: RunReport, path: Path) -> None:
    """Atomic replacement avoids presenting a partially serialized report as evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report.model_dump(mode="json"), stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
