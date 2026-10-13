"""Run a bounded control lifecycle alongside traffic on one monotonic timeline."""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

import httpx

from bront.checks import evaluate_recovery, observe_recovery, traffic_windows
from bront.kubernetes_scenarios import KubernetesDriver
from bront.measurement import LoadResult
from bront.proxy_scenarios import ProxyDriver
from bront.results import AssertionResult, Phase
from bront.scenario_models import (
    KubernetesRestart,
    ProxyDelay,
    ProxyOutage,
    ScenarioConfig,
    ScenarioError,
    ScenarioEvent,
    ScenarioEvidence,
)
from bront.suites import Assertions


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
        windows = traffic_windows(phase, self.evidence)
        self.evidence.observations["traffic_windows"] = {
            key: value for key, value in windows.items()
        }
        if not all(windows.values()):
            raise ScenarioError("insufficient_traffic_overlap")

    def recovery(
        self, phase: Phase, policy: Assertions, has_identity: bool
    ) -> list[AssertionResult]:
        recovery = observe_recovery(phase, self.evidence.events, policy, has_identity)
        self.evidence.recovery = recovery
        return evaluate_recovery(recovery, policy) if recovery is not None else []
