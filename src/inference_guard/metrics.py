from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self) -> None:
        # Each app instance owns its registry, including during tests.
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "inferenceguard_requests_total",
            "Validated inference requests by final outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.attempts = Counter(
            "inferenceguard_backend_attempts_total",
            "Backend attempts by backend and outcome",
            ["backend", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "inferenceguard_backend_duration_seconds",
            "Backend attempt latency, including failed attempts",
            ["backend"],
            registry=self.registry,
        )

        self.circuit_state = Gauge(
            "inferenceguard_circuit_state",
            "Observed circuit state, one active state per backend; not backend health",
            ["backend", "state"],
            registry=self.registry,
        )
        self.transitions = Counter(
            "inferenceguard_circuit_transitions_total",
            "Actual circuit state transitions",
            ["backend", "from_state", "to_state"],
            registry=self.registry,
        )
        self.recovery = Histogram(
            "inferenceguard_circuit_recovery_duration_seconds",
            "Time from first opening until a successful recovery probe, including retries",
            ["backend"],
            buckets=(1, 5, 10, 15, 30, 60, 120, 300),
            registry=self.registry,
        )
        self.last_recovery = Gauge(
            "inferenceguard_circuit_last_recovery_seconds",
            "Duration of last completed open-to-closed episode; absent until first recovery",
            ["backend"],
            registry=self.registry,
        )

    def watch_circuit(self, backend: str) -> Callable[[str, str, float], None]:
        opened_at: float | None = None
        for state in ("closed", "open", "half_open"):
            self.circuit_state.labels(backend, state).set(int(state == "closed"))
        # Initialize outcome counters so ratios include a zero-error/fallback baseline.
        for outcome in ("success", "error"):
            self.attempts.labels(backend, outcome)
        for outcome in ("success", "unavailable"):
            self.requests.labels(outcome)

        def transition(previous: str, current: str, now: float) -> None:
            nonlocal opened_at
            self.circuit_state.labels(backend, previous).set(0)
            self.circuit_state.labels(backend, current).set(1)
            self.transitions.labels(backend, previous, current).inc()
            if current == "open" and opened_at is None:
                opened_at = now
            elif current == "closed" and opened_at is not None:
                elapsed = now - opened_at
                self.recovery.labels(backend).observe(elapsed)
                self.last_recovery.labels(backend).set(elapsed)
                opened_at = None

        return transition
