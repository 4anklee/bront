"""Explicit release checks, also used to verify assertions when reading reports."""

from inference_guard.results import AssertionResult, Phase
from inference_guard.scenario_models import RecoveryEvidence, ScenarioEvent, ScenarioEvidence
from inference_guard.suites import Assertions


def evaluate_measurement(phase: Phase, policy: Assertions) -> list[AssertionResult]:
    values = [
        ("max_failed_requests", phase.failed, policy.max_failed_requests),
        ("max_p95_ms", phase.latency_ms.get("p95"), policy.max_p95_ms),
        ("max_p99_ms", phase.latency_ms.get("p99"), policy.max_p99_ms),
        (
            "max_error_rate",
            phase.failed / phase.requests if phase.requests else None,
            policy.max_error_rate,
        ),
        (
            "max_validation_failures",
            phase.errors.get("validation", 0),
            policy.max_validation_failures,
        ),
    ]
    return [
        AssertionResult(
            name=name, actual=actual, limit=limit, passed=actual is not None and actual <= limit
        )
        for name, actual, limit in values
        if limit is not None
    ]


def evaluate_recovery(recovery: RecoveryEvidence, policy: Assertions) -> list[AssertionResult]:
    results = []
    for name, observed, limit in (
        (
            "recovery_within_seconds",
            recovery.first_valid_response_ms,
            policy.recovery_within_seconds,
        ),
        (
            "preferred_backend_within_seconds",
            recovery.first_preferred_backend_ms,
            policy.preferred_backend_within_seconds,
        ),
    ):
        if limit is not None:
            actual = observed / 1000 if observed is not None else None
            results.append(
                AssertionResult(
                    name=name,
                    actual=actual,
                    limit=limit,
                    passed=actual is not None and actual <= limit,
                )
            )
    return results


def observe_recovery(
    phase: Phase, events: list[ScenarioEvent], policy: Assertions, has_identity: bool
) -> RecoveryEvidence | None:
    restores = [e for e in events if e.action == "restore" and e.status == "completed"]
    if not restores:
        return None
    start = restores[-1].finished_ms
    if start is None:
        return None
    recovery = RecoveryEvidence(
        from_ms=start,
        backend_identity_available=has_identity,
        preferred_backend=policy.preferred_backend,
    )
    valid = [s for s in phase.samples if s.error is None and s.started_ms >= start]
    if valid:
        recovery.first_valid_response_ms = min(s.started_ms + s.duration_ms for s in valid) - start
    preferred = [
        s
        for s in valid
        if policy.preferred_backend is not None and s.backend == policy.preferred_backend
    ]
    if preferred:
        recovery.first_preferred_backend_ms = (
            min(s.started_ms + s.duration_ms for s in preferred) - start
        )
    return recovery


def traffic_windows(phase: Phase, scenario: ScenarioEvidence) -> dict[str, int]:
    events = {event.action: event for event in scenario.events}
    applied = events["apply"]
    reversible = scenario.kind != "kubernetes-restart"
    terminal = events["restored" if reversible else "observed"]
    end = terminal.finished_ms
    during_start = events["observed"].finished_ms if reversible else applied.finished_ms
    during_end = events["restore"].started_ms if reversible else end
    if end is None or during_start is None or during_end is None:
        raise ValueError("scenario_events_incomplete")
    starts = [sample.started_ms for sample in phase.samples]
    return {
        "before": sum(start < applied.started_ms for start in starts),
        "during": sum(during_start <= start <= during_end for start in starts),
        "after": sum(start > end for start in starts),
    }
