"""Run a bounded control lifecycle alongside traffic on one monotonic timeline."""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

import httpx

from inference_guard.kubernetes_scenarios import KubernetesDriver
from inference_guard.measurement import LoadResult
from inference_guard.proxy_scenarios import ProxyDriver
from inference_guard.results import AssertionResult, Phase
from inference_guard.scenario_models import (
    KubernetesRestart,
    ProxyDelay,
    ProxyOutage,
    RecoveryEvidence,
    ScenarioConfig,
    ScenarioError,
    ScenarioEvent,
    ScenarioEvidence,
)
from inference_guard.suites import Assertions


class Driver(Protocol):
    reversible: bool
    attempted: bool

    async def preflight(self) -> None: ...
    async def apply(self) -> None: ...
    async def observe(self, *, restored: bool = False) -> None: ...
    async def restore(self) -> None: ...


def failure_code(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return failure_code(exc.exceptions[0])
    return str(exc) if isinstance(exc, ScenarioError) else type(exc).__name__


class ScenarioSession:
    def __init__(
        self,
        config: ScenarioConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        require_identity: bool = False,
    ):
        self.config = config
        self.require_identity = require_identity
        self.origin = monotonic()
        # Control URLs can expose infrastructure paths; retain only the configured object name.
        self.evidence = ScenarioEvidence(
            kind=config.kind, configuration=config.model_dump(mode="json", exclude={"url"})
        )
        self.client: httpx.AsyncClient | None = None
        self.driver: Driver
        if isinstance(config, (ProxyOutage, ProxyDelay)):
            self.client = httpx.AsyncClient(
                timeout=config.control_timeout_seconds, transport=transport, trust_env=False
            )
            self.driver = ProxyDriver(config, self.evidence, self.client)
        else:
            self.driver = KubernetesDriver(config, self.evidence)

    async def event(
        self,
        action: str,
        operation: Callable[[], Awaitable[None]],
        *,
        scheduled: float | None = None,
        observe: bool = False,
    ) -> None:
        event = ScenarioEvent(
            action=action, scheduled_ms=scheduled, started_ms=(monotonic() - self.origin) * 1000
        )
        self.evidence.events.append(event)
        try:
            timeout = (
                self.config.observe_timeout_seconds
                if observe
                else self.config.control_timeout_seconds
            )
            async with asyncio.timeout(timeout):
                await operation()
            event.status = "completed"
        except asyncio.CancelledError:
            event.status = "cancelled"
            raise
        except Exception as exc:
            event.status = "failed"
            event.error = failure_code(exc)
            raise
        finally:
            event.finished_ms = (monotonic() - self.origin) * 1000

    async def preflight(self) -> None:
        await self.event("preflight", self.driver.preflight, observe=True)

    async def cleanup(self, scheduled: float | None = None) -> bool:
        if not self.driver.attempted:
            self.evidence.cleanup = "not_required"
            return True
        if not self.driver.reversible:
            self.evidence.cleanup = "not_reversible"
            return True
        if self.evidence.cleanup == "restored":
            return True
        self.evidence.cleanup = "pending"
        try:
            await self.event("restore", self.driver.restore, scheduled=scheduled)
            await self.event("restored", lambda: self.driver.observe(restored=True), observe=True)
            self.evidence.cleanup = "restored"
            return True
        except Exception:
            self.evidence.cleanup = "failed"
            return False

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def measure(
        self, load: Callable[[], Awaitable[LoadResult]], origin: float, phase: Phase
    ) -> LoadResult:
        shift = (origin - self.origin) * 1000
        for event in self.evidence.events:
            event.started_ms -= shift
            if event.finished_ms is not None:
                event.finished_ms -= shift
        self.origin = origin
        self.evidence.clock_origin = "measurement"
        action_done = False

        async def lifecycle() -> None:
            nonlocal action_done
            await asyncio.sleep(max(0, origin + self.config.at_seconds - monotonic()))
            if not phase.samples:
                raise ScenarioError("no_traffic_before_action")
            if self.require_identity and not any(
                sample.error is None and sample.backend is not None for sample in phase.samples
            ):
                raise ScenarioError("backend_identity_not_observed_before_action")
            await self.event("apply", self.driver.apply, scheduled=self.config.at_seconds * 1000)
            await self.event("observed", self.driver.observe, observe=True)
            if not isinstance(self.config, KubernetesRestart):
                restore_at = (monotonic() - origin + self.config.hold_seconds) * 1000
                await asyncio.sleep(self.config.hold_seconds)
                if not await self.cleanup(scheduled=restore_at):
                    raise ScenarioError("scenario_cleanup_failed")
            action_done = True

        async def traffic() -> LoadResult:
            result = await load()
            if not action_done:
                raise ScenarioError("traffic_ended_before_scenario_completed")
            return result

        async with asyncio.TaskGroup() as group:
            task = group.create_task(traffic())
            group.create_task(lifecycle())
        self.verify_overlap(phase)
        self.evidence.completed = True
        return task.result()

    def verify_overlap(self, phase: Phase) -> None:
        applied = next(e for e in self.evidence.events if e.action == "apply")
        terminal = next(
            e
            for e in self.evidence.events
            if e.action == ("restored" if self.driver.reversible else "observed")
        )
        end = terminal.finished_ms
        assert end is not None and applied.finished_ms is not None
        during_start = applied.finished_ms
        during_end = end
        if self.driver.reversible:
            observed = next(e for e in self.evidence.events if e.action == "observed")
            restore = next(e for e in self.evidence.events if e.action == "restore")
            assert observed.finished_ms is not None
            during_start, during_end = observed.finished_ms, restore.started_ms
        starts = [s.started_ms for s in phase.samples]
        windows = {
            "before": sum(start < applied.started_ms for start in starts),
            "during": sum(during_start <= start <= during_end for start in starts),
            "after": sum(start > end for start in starts),
        }
        self.evidence.observations["traffic_windows"] = {
            key: value for key, value in windows.items()
        }
        if not all(windows.values()):
            raise ScenarioError("insufficient_traffic_overlap")

    def recovery(
        self, phase: Phase, policy: Assertions, has_identity: bool
    ) -> list[AssertionResult]:
        restores = [
            e for e in self.evidence.events if e.action == "restore" and e.status == "completed"
        ]
        if not restores:
            return []
        start = restores[-1].finished_ms
        assert start is not None
        recovery = RecoveryEvidence(
            from_ms=start,
            backend_identity_available=has_identity,
            preferred_backend=policy.preferred_backend,
        )
        valid = [s for s in phase.samples if s.error is None and s.started_ms >= start]
        if valid:
            recovery.first_valid_response_ms = (
                min(s.started_ms + s.duration_ms for s in valid) - start
            )
        preferred = [
            s
            for s in valid
            if policy.preferred_backend is not None and s.backend == policy.preferred_backend
        ]
        if preferred:
            recovery.first_preferred_backend_ms = (
                min(s.started_ms + s.duration_ms for s in preferred) - start
            )
        self.evidence.recovery = recovery
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
