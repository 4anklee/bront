from prometheus_client import CollectorRegistry, Counter, Histogram


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
