"""Conservative release verdicts from compatible, completed, repeated experiments."""

import math
from pathlib import Path
from statistics import median

from pydantic import JsonValue

from inference_guard.artifacts import write_artifact
from inference_guard.comparison_models import (
    ComparisonCheck,
    ComparisonPolicy,
    ComparisonReport,
    Difference,
    Distribution,
    MetricComparison,
)
from inference_guard.evidence import Evidence, ReadBudget, load_evidence
from inference_guard.results import RunReport
from inference_guard.suites import SuiteError


def context(report: RunReport) -> dict[str, JsonValue]:
    return {
        "contract": report.provenance.contract,
        "runner": {key: value for key, value in report.provenance.runner.items()},
        "deployment": report.provenance.deployment.model_dump(mode="json"),
    }


def differences(left: JsonValue, right: JsonValue, path: str = "") -> list[Difference]:
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(left.keys() | right.keys()):
            location = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                result.append(
                    Difference(field=location, baseline=left.get(key), candidate=right.get(key))
                )
            else:
                result.extend(differences(left[key], right[key], location))
        return result
    return [Difference(field=path, baseline=left, candidate=right)]


def distribution(values: list[float]) -> Distribution:
    return Distribution(
        values=values, minimum=min(values), median=median(values), maximum=max(values)
    )


def percent_change(base: float, candidate: float) -> float | None:
    if base == 0:
        return 0 if candidate == 0 else None
    result = (candidate / base - 1) * 100
    return result if math.isfinite(result) else None


def metric_checks(
    report: ComparisonReport,
    baseline: list[RunReport],
    candidate: list[RunReport],
    *,
    gate: bool = True,
) -> None:
    policy = report.policy
    metrics = (
        (
            "p95_ms",
            "ms",
            lambda r: r.measurement.latency_ms["p95"],
            policy.max_p95_increase_percent,
            False,
        ),
        (
            "p99_ms",
            "ms",
            lambda r: r.measurement.latency_ms["p99"],
            policy.max_p99_increase_percent,
            False,
        ),
        (
            "requests_per_second",
            "requests/s",
            lambda r: r.measurement.requests_per_second,
            policy.max_throughput_drop_percent,
            True,
        ),
        (
            "error_rate",
            "fraction",
            lambda r: r.measurement.failed / r.measurement.requests,
            policy.max_error_rate_increase,
            False,
        ),
    )
    for name, unit, extract, limit, reverse in metrics:
        left = distribution([extract(run) for run in baseline])
        right = distribution([extract(run) for run in candidate])
        report.metrics[name] = MetricComparison(
            unit=unit,
            baseline=left,
            candidate=right,
            median_change_percent=percent_change(left.median, right.median),
        )
        if limit is None or not gate:
            continue
        changes = []
        for before in (left.minimum, left.maximum):
            for after in (right.minimum, right.maximum):
                value = after - before if name == "error_rate" else percent_change(before, after)
                changes.append(-value if reverse and value is not None else value)
        if any(value is None for value in changes):
            report.checks.append(
                ComparisonCheck(
                    name=name,
                    status="inconclusive",
                    limit=limit,
                    observed_lower=None,
                    observed_upper=None,
                    reason="relative_change_undefined",
                )
            )
            continue
        defined = [value for value in changes if value is not None]
        lower, upper = min(defined), max(defined)
        verdict = "passed" if upper <= limit else "failed" if lower > limit else "inconclusive"
        report.checks.append(
            ComparisonCheck(
                name=name,
                status=verdict,
                limit=limit,
                observed_lower=lower,
                observed_upper=upper,
                reason="variation_crosses_limit" if verdict == "inconclusive" else None,
            )
        )


def compare_evidence(
    baseline: Evidence, candidate: Evidence, policy: ComparisonPolicy
) -> ComparisonReport:
    report = ComparisonReport(policy=policy, issues=[*baseline.issues, *candidate.issues])
    report.baseline_run_ids = [r.provenance.run_id for r in baseline.runs]
    report.candidate_run_ids = [r.provenance.run_id for r in candidate.runs]
    ids = report.baseline_run_ids + report.candidate_run_ids
    if len(set(ids)) != len(ids):
        report.issues.append("duplicate_run_identity")
    for side, evidence in (("baseline", baseline), ("candidate", candidate)):
        if len(evidence.runs) < policy.minimum_runs:
            report.issues.append(f"{side}:insufficient_repeats")
        for index, run in enumerate(evidence.runs):
            label = f"{side}[{index}]"
            report.run_summaries.append(
                {
                    "side": side,
                    "run_id": run.provenance.run_id,
                    "status": run.status,
                    "requests": run.measurement.requests,
                    "failed": run.measurement.failed,
                    "errors": {key: value for key, value in run.measurement.errors.items()},
                    "failed_assertions": [a.name for a in run.assertions if not a.passed],
                }
            )
            if run.status not in ("passed", "failed") or run.reason is not None:
                report.issues.append(f"{label}:run_incomplete")
            for missing in run.provenance.deployment.missing():
                report.issues.append(f"{label}:missing_metadata:{missing}")
            for key in policy.required_labels:
                if not run.provenance.deployment.labels.get(key):
                    report.issues.append(f"{label}:required_label_missing:{key}")
            if run.measurement.requests < policy.minimum_requests:
                report.issues.append(f"{label}:insufficient_requests")
            if run.measurement.elapsed_seconds < policy.minimum_duration_seconds:
                report.issues.append(f"{label}:insufficient_duration")
            if side == "baseline" and run.status != "passed":
                report.issues.append(f"{label}:baseline_assertions_not_passed")
    if not baseline.runs or not candidate.runs:
        return report
    report.baseline_context = context(baseline.runs[0])
    report.candidate_context = context(candidate.runs[0])
    for side, evidence in (("baseline", baseline), ("candidate", candidate)):
        for index, run in enumerate(evidence.runs[1:], 1):
            for difference in differences(context(evidence.runs[0]), context(run)):
                difference.field = f"{side}[{index}].{difference.field}"
                report.differences.append(difference)
    for difference in differences(report.baseline_context, report.candidate_context):
        difference.allowed = any(
            difference.field == path or difference.field.startswith(path + ".")
            for path in policy.allowed_changes
        )
        report.differences.append(difference)
    if any(not d.allowed for d in report.differences):
        report.status = "incompatible"
        return report
    complete = all(
        run.status in ("passed", "failed") and run.reason is None and run.measurement.requests > 0
        for run in baseline.runs + candidate.runs
    )
    if complete:
        metric_checks(report, baseline.runs, candidate.runs, gate=not report.issues)
    if report.issues:
        return report
    if any(r.status == "failed" for r in candidate.runs) or any(
        c.status == "failed" for c in report.checks
    ):
        report.status = "failed"
    elif any(c.status == "inconclusive" for c in report.checks):
        report.status = "inconclusive"
    else:
        report.status = "passed"
    return report


def compare_files(
    baseline: list[Path],
    candidate: list[Path],
    policy: ComparisonPolicy,
    output: Path | None = None,
) -> ComparisonReport:
    budget = ReadBudget()
    left = load_evidence(baseline, "baseline", budget)
    right = load_evidence(candidate, "candidate", budget)
    report = compare_evidence(left, right, policy)
    if output is not None:
        if output.resolve() in budget.paths:
            raise SuiteError("Comparison output must not overwrite an input artifact")
        write_artifact(report, output)
    return report
