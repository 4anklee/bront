"""Bounded artifact loading and consistency checks before release comparison."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from bront.checks import (
    evaluate_measurement,
    evaluate_recovery,
    observe_recovery,
    traffic_windows,
)
from bront.provenance import digest
from bront.results import Phase, RunReport
from bront.series import SeriesReport
from bront.suites import Suite
from bront.validation import parse_json


@dataclass
class ReadBudget:
    remaining: int = 256 * 1024 * 1024
    paths: set[Path] = field(default_factory=set)

    def read(self, path: Path) -> dict:
        source = path.resolve()
        if source in self.paths:
            raise ValueError("duplicate_input_path")
        self.paths.add(source)
        maximum = min(self.remaining, 128 * 1024 * 1024)
        with source.open("rb") as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("input_size_limit")
        self.remaining -= len(content)
        value = parse_json(content)
        if not isinstance(value, dict):
            raise ValueError("invalid_artifact")
        return value


def check_phase(phase: Phase, names: list[str]) -> None:
    if phase.elapsed_seconds < 0 or (phase.samples and phase.elapsed_seconds == 0):
        raise ValueError("invalid_elapsed_time")
    for index, sample in enumerate(phase.samples):
        if sample.index != index or sample.case != names[index % len(names)]:
            raise ValueError("invalid_sample_sequence")
        if (
            sample.started_ms < 0
            or sample.duration_ms < 0
            or sample.started_ms + sample.duration_ms > phase.elapsed_seconds * 1000 + 0.01
        ):
            raise ValueError("invalid_sample_timing")
        if sample.error is None and sample.status_code is None:
            raise ValueError("successful_sample_missing_status")
    if phase == Phase():
        return  # Initial running snapshots precede the first summary calculation.
    recalculated = phase.model_copy(deep=True)
    recalculated.summarize()
    if recalculated.model_dump(exclude={"samples"}) != phase.model_dump(exclude={"samples"}):
        raise ValueError("summary_does_not_match_samples")


def validate_run(raw: dict) -> RunReport:
    if raw.get("report_type") != "bront.run" or raw.get("schema_version") != 3:
        raise ValueError("run_schema_3_required")
    report = RunReport.model_validate(raw)
    provenance = report.provenance
    UUID(provenance.run_id)
    contract = provenance.contract
    if set(contract) != {
        "adapter",
        "method",
        "route_sha256",
        "headers",
        "backend",
        "cases_sha256",
        "case_names",
        "workload",
        "assertions",
        "scenario",
        "measurement",
    }:
        raise ValueError("incomplete_comparison_contract")
    if digest(contract) != provenance.contract_sha256:
        raise ValueError("contract_fingerprint_mismatch")
    if set(provenance.runner) != {
        "version",
        "source_sha256",
        "python",
        "architecture",
        "system",
        "httpx",
        "pydantic",
    }:
        raise ValueError("runner_identity_missing")
    if provenance.missing_metadata != provenance.deployment.missing():
        raise ValueError("metadata_completeness_mismatch")
    if (
        contract.get("cases_sha256") != report.cases_sha256
        or contract.get("workload") != report.workload
        or contract.get("adapter") != report.target.get("adapter")
        or contract.get("method") != report.target.get("method")
    ):
        raise ValueError("contract_does_not_match_run")
    if contract.get("measurement") != {
        "load": "closed-loop",
        "latency": "all-attempts",
        "percentile": "nearest-rank",
    }:
        raise ValueError("unsupported_measurement_semantics")
    names = contract.get("case_names")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError("case_names_missing")
    names = [name for name in names if isinstance(name, str)]
    check_phase(report.warmup, names)
    check_phase(report.measurement, names)
    scenario_config = contract.get("scenario")
    if isinstance(scenario_config, dict) and scenario_config.get("kind") in (
        "proxy-outage",
        "proxy-delay",
    ):
        scenario_config = {**scenario_config, "url": "http://redacted.invalid"}
    declared = Suite.model_validate(
        {
            "version": 1,
            "target": {
                "url": report.target.get("origin"),
                "method": contract["method"],
                "adapter": contract["adapter"],
                "backend": contract["backend"],
            },
            "workload": {**report.workload, "cases": "recorded"},
            "assertions": contract["assertions"],
            "scenario": scenario_config,
        }
    )
    workload, policy = declared.workload, declared.assertions
    if (contract.get("scenario") is None) != (report.scenario is None):
        raise ValueError("scenario_contract_mismatch")
    if report.scenario is not None and report.scenario.configuration != contract.get("scenario"):
        raise ValueError("scenario_contract_mismatch")
    if report.status not in ("passed", "failed") or report.reason is not None:
        return report  # Retain partial evidence, but it cannot qualify for a comparison verdict.
    if report.finished_at is None:
        raise ValueError("completion_time_missing")
    started, finished = (
        datetime.fromisoformat(report.started_at),
        datetime.fromisoformat(report.finished_at),
    )
    if started.tzinfo is None or finished.tzinfo is None or finished < started:
        raise ValueError("invalid_run_timestamps")
    if report.warmup.requests != workload.warmup_requests or report.warmup.failed:
        raise ValueError("warmup_incomplete")
    measured = report.measurement
    if (
        measured.requests == 0
        or measured.errors.get("cancelled")
        or measured.errors.get("execution")
    ):
        raise ValueError("measurement_incomplete")
    if workload.requests is not None and measured.requests != workload.requests:
        raise ValueError("request_count_incomplete")
    if (
        workload.duration_seconds is not None
        and measured.elapsed_seconds < workload.duration_seconds
    ):
        raise ValueError("duration_incomplete")
    expected = evaluate_measurement(measured, policy)
    scenario = report.scenario
    if scenario is not None:
        if (
            scenario.kind != scenario.configuration.get("kind")
            or scenario.clock_origin != "measurement"
        ):
            raise ValueError("invalid_scenario_identity")
        reversible = scenario.kind != "kubernetes-restart"
        if not scenario.completed or scenario.cleanup != (
            "restored" if reversible else "not_reversible"
        ):
            raise ValueError("scenario_incomplete")
        actions = ["preflight", "apply", "observed"] + (
            ["restore", "restored"] if reversible else []
        )
        if [event.action for event in scenario.events] != actions or any(
            event.status != "completed" or event.finished_ms is None for event in scenario.events
        ):
            raise ValueError("scenario_events_incomplete")
        last = None
        for event in scenario.events:
            end = event.finished_ms
            if (
                end is None
                or event.started_ms > end
                or (last is not None and event.started_ms < last)
            ):
                raise ValueError("invalid_scenario_timeline")
            last = end
        if last is None or last > measured.elapsed_seconds * 1000 + 0.01:
            raise ValueError("scenario_outside_measurement")
        applied = scenario.events[1]
        scheduled = scenario.configuration["at_seconds"]
        if not isinstance(scheduled, (float, int)) or applied.scheduled_ms != scheduled * 1000:
            raise ValueError("scenario_schedule_mismatch")
        if applied.scheduled_ms is None or applied.started_ms + 0.01 < applied.scheduled_ms:
            raise ValueError("scenario_started_before_schedule")
        if reversible:
            observed, restore = scenario.events[2:4]
            hold = scenario.configuration["hold_seconds"]
            if not isinstance(hold, (float, int)) or observed.finished_ms is None:
                raise ValueError("invalid_fault_hold")
            if restore.started_ms + 0.01 < observed.finished_ms + hold * 1000:
                raise ValueError("fault_hold_incomplete")
        windows = traffic_windows(measured, scenario)
        if not all(windows.values()) or windows != scenario.observations.get("traffic_windows"):
            raise ValueError("scenario_overlap_mismatch")
        recovery = observe_recovery(
            measured, scenario.events, policy, contract.get("backend") is not None
        )
        if recovery != scenario.recovery:
            raise ValueError("recovery_does_not_match_samples")
        if recovery is not None:
            expected.extend(evaluate_recovery(recovery, policy))
    if report.assertions != expected or (report.status == "passed") != all(
        a.passed for a in expected
    ):
        raise ValueError("assertions_do_not_match_samples")
    return report


@dataclass
class Evidence:
    runs: list[RunReport] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def load_evidence(paths: list[Path], side: str, budget: ReadBudget) -> Evidence:
    evidence = Evidence()
    if not paths or len(paths) > 100:
        evidence.issues.append(f"{side}:provide_1_to_100_inputs")
        return evidence
    for index, path in enumerate(paths):
        if len(evidence.runs) >= 100:
            evidence.issues.append(f"{side}:too_many_runs")
            break
        label = f"{side}[{index}]"
        try:
            raw = budget.read(path)
            if raw.get("report_type") == "bront.series":
                series = SeriesReport.model_validate(raw)
                if (
                    series.finished_at is None
                    or len(series.runs) != series.requested_runs
                    or series.status
                    not in (
                        "passed",
                        "failed",
                    )
                ):
                    evidence.issues.append(f"{label}:series_incomplete")
                if len(series.runs) + len(evidence.runs) > 100:
                    raise ValueError("too_many_runs")
                children = []
                for reference in series.runs:
                    child = (path.parent / reference.path).resolve()
                    if Path(reference.path).is_absolute() or not child.is_relative_to(
                        path.parent.resolve()
                    ):
                        raise ValueError("series_reference_outside_directory")
                    value = budget.read(child)
                    if digest(value) != reference.sha256:
                        raise ValueError("series_digest_mismatch")
                    report = validate_run(value)
                    if report.provenance.run_id != reference.run_id:
                        raise ValueError("series_run_identity_mismatch")
                    children.append(report)
                if series.status in ("passed", "failed"):
                    expected_status = (
                        "passed" if all(r.status == "passed" for r in children) else "failed"
                    )
                    if series.status != expected_status:
                        evidence.issues.append(f"{label}:series_status_mismatch")
                evidence.runs.extend(children)
            else:
                evidence.runs.append(validate_run(raw))
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            # Only our small failure codes are safe; Pydantic/JSON/OS messages include inputs.
            code = (
                str(exc)
                if type(exc) is ValueError and str(exc).isidentifier()
                else type(exc).__name__
            )
            evidence.issues.append(f"{label}:{code}")
            if code == "too_many_runs":
                break
    return evidence
