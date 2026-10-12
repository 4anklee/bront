from collections.abc import Callable
from time import monotonic


class CircuitBreaker:
    """Request-driven breaker owned by one asyncio event loop.

    Generation tickets prevent late in-flight completions from closing a newer
    open circuit. Only one request may probe a circuit after its cooldown.
    """

    def __init__(
        self,
        failure_threshold: int,
        recovery_timeout: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.clock = clock
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.generation = 0

    def acquire(self) -> int | None:
        if self.state == "open":
            if self.clock() - self.opened_at < self.recovery_timeout:
                return None
            self.state = "half_open"
            return self.generation
        if self.state == "half_open":
            return None
        return self.generation

    def succeed(self, ticket: int) -> None:
        if ticket != self.generation:
            return
        if self.state == "half_open":
            self.generation += 1
        self.state = "closed"
        self.failures = 0

    def fail(self, ticket: int) -> None:
        if ticket != self.generation:
            return
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = self.clock()
            self.generation += 1

    def snapshot(self) -> dict[str, str | int]:
        return {"state": self.state, "consecutive_failures": self.failures}
