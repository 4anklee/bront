"""Comparison policy and evidence models. No statistical significance claim is implied."""

import re
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from inference_guard.results import timestamp
from inference_guard.validation import StrictModel


class ComparisonPolicy(StrictModel):
    version: Literal[1]
    minimum_runs: int = Field(default=3, ge=3, le=100)
    minimum_requests: int = Field(default=100, ge=1, le=1_000_000)
    minimum_duration_seconds: float = Field(default=0, ge=0, le=3600)
    required_labels: list[str] = Field(default_factory=list, max_length=100)
    allowed_changes: list[str] = Field(default_factory=list, max_length=100)
    max_p95_increase_percent: float | None = Field(default=None, ge=0)
    max_p99_increase_percent: float | None = Field(default=None, ge=0)
    max_throughput_drop_percent: float | None = Field(default=None, ge=0, le=100)
    max_error_rate_increase: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def intentional_changes(self) -> Self:
        permitted = (
            r"deployment\.(model_revision|serving_revision|architecture|resources(\.[\w.-]+)?|labels\.[\w.-]+)"
            r"|contract\.(route_sha256|scenario\.(context|namespace|deployment|proxy))"
        )
        if any(not re.fullmatch(permitted, path) for path in self.allowed_changes):
            raise ValueError(
                "Only deployment metadata, route, and scenario scope changes can be allowed"
            )
        if len(set(self.allowed_changes)) != len(self.allowed_changes):
            raise ValueError("Duplicate allowed change")
        return self


class Distribution(StrictModel):
    values: list[float]
    minimum: float
    median: float
    maximum: float


class MetricComparison(StrictModel):
    unit: str
    baseline: Distribution
    candidate: Distribution
    median_change_percent: float | None


class ComparisonCheck(StrictModel):
    name: str
    status: Literal["passed", "failed", "inconclusive"]
    limit: float
    observed_lower: float | None
    observed_upper: float | None
    reason: str | None = None


class Difference(StrictModel):
    field: str
    baseline: JsonValue
    candidate: JsonValue
    allowed: bool = False


class ComparisonReport(StrictModel):
    report_type: Literal["bront.comparison"] = "bront.comparison"
    schema_version: Literal[1] = 1
    created_at: str = Field(default_factory=timestamp)
    status: Literal["passed", "failed", "inconclusive", "incompatible"] = "inconclusive"
    policy: ComparisonPolicy
    baseline_run_ids: list[str] = Field(default_factory=list)
    candidate_run_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    differences: list[Difference] = Field(default_factory=list)
    baseline_context: dict[str, JsonValue] = Field(default_factory=dict)
    candidate_context: dict[str, JsonValue] = Field(default_factory=dict)
    run_summaries: list[dict[str, JsonValue]] = Field(default_factory=list)
    metrics: dict[str, MetricComparison] = Field(default_factory=dict)
    checks: list[ComparisonCheck] = Field(default_factory=list)
