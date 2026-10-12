"""Bounded-concurrency HTTP measurements and reproducible fault scenarios."""

import asyncio
import math
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from inference_guard.faults import FaultController
from inference_guard.schemas import InferenceResponse

PAYLOAD: dict[str, object] = {"model": "double", "inputs": [1.0, 2.0, 3.0]}


class Scenario(StrEnum):
    baseline = "baseline"
    outage = "outage"
    slow_backend = "slow-backend"
    recovery = "recovery"
    all = "all"


class BenchmarkConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    gateway_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    toxiproxy_url: HttpUrl = HttpUrl("http://127.0.0.1:18474")
    scenario: Scenario = Scenario.baseline
    requests: int = Field(default=100, ge=1, le=1_000_000)
    concurrency: int = Field(default=10, ge=1, le=1000)
    warmup: int = Field(default=10, ge=0, le=10000)
    timeout_seconds: float = Field(default=5, gt=0, le=300)
    latency_ms: int = Field(default=2000, ge=1, le=30000)
    recovery_timeout_seconds: float = Field(default=30, gt=0, le=300)


class GatewayConfiguration(BaseModel):
    primary_protocol: Literal["mock", "triton"]
    request_timeout_seconds: float = Field(gt=0)
    failure_threshold: int = Field(ge=1)
    recovery_timeout_seconds: float = Field(gt=0)


class Sample(BaseModel):
    index: int
    started_ms: float
    duration_ms: float
    status_code: int | None = None
    backend: Literal["primary", "fallback"] | None = None
    error: str | None = None


class PhaseResult(BaseModel):
    scenario: Scenario
    requests: int
    successful: int
    failed: int
    success_rate: float
    primary_requests: int
    fallback_requests: int
    elapsed_seconds: float
    attempted_rps: float
    successful_rps: float
    latency_ms: dict[str, float]
    errors: dict[str, int]
    expectations_met: bool = False
    recovery_seconds: float | None = None
    recovery_probes: list[Sample] = Field(default_factory=list)
    samples: list[Sample]


class BenchmarkReport(BaseModel):
    schema_version: int = 1
    started_at: str
    config: BenchmarkConfig
    gateway_configuration: GatewayConfiguration
    payload: dict[str, object] = Field(default_factory=lambda: dict(PAYLOAD))
    phases: list[PhaseResult]
    passed: bool


def percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile; the observed maximum is p100."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def summarize(scenario: Scenario, samples: list[Sample], elapsed: float) -> PhaseResult:
    successful = sum(sample.error is None for sample in samples)
    durations = [sample.duration_ms for sample in samples]
    return PhaseResult(
        scenario=scenario,
        requests=len(samples),
        successful=successful,
        failed=len(samples) - successful,
        success_rate=successful / len(samples),
        primary_requests=sum(sample.backend == "primary" for sample in samples),
        fallback_requests=sum(sample.backend == "fallback" for sample in samples),
        elapsed_seconds=elapsed,
        attempted_rps=len(samples) / elapsed if elapsed else 0,
        successful_rps=successful / elapsed if elapsed else 0,
        latency_ms={
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
            "p99": percentile(durations, 99),
            "mean": sum(durations) / len(durations),
            "max": max(durations),
        },
        errors=dict(Counter(sample.error for sample in samples if sample.error)),
        samples=samples,
    )


class BenchmarkRunner:
    def __init__(
        self,
        config: BenchmarkConfig,
        client: httpx.AsyncClient,
        faults: FaultController | None = None,
        progress: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.config = config
        self.client = client
        self.faults = faults
        self.progress = progress
        self.url = str(config.gateway_url).rstrip("/")

    async def request(self, index: int, origin: float) -> Sample:
        started = monotonic()
        sample = Sample(index=index, started_ms=(started - origin) * 1000, duration_ms=0)
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                response = await self.client.post(self.url + "/v1/infer", json=PAYLOAD)
                sample.status_code = response.status_code
                if response.status_code != 200:
                    sample.error = f"http_{response.status_code}"
                else:
                    result = InferenceResponse.model_validate(response.json())
                    if result.outputs != [2, 4, 6] or result.backend not in ("primary", "fallback"):
                        raise ValueError("Unexpected inference result")
                    sample.backend = result.backend
        except TimeoutError, httpx.TimeoutException:
            sample.error = "timeout"
        except httpx.TransportError:
            sample.error = "transport_error"
        except ValueError:
            sample.error = "invalid_response"
        sample.duration_ms = (monotonic() - started) * 1000
        return sample

    async def load(
        self, scenario: Scenario, count: int, origin: float | None = None
    ) -> PhaseResult:
        started = monotonic() if origin is None else origin
        indices = iter(range(count))
        samples: list[Sample] = []

        async def worker() -> None:
            # No await between obtaining an index and owning it; only C tasks are created.
            for index in indices:
                samples.append(await self.request(index, started))

        async with asyncio.TaskGroup() as group:
            for _ in range(min(self.config.concurrency, count)):
                group.create_task(worker())
        return summarize(scenario, sorted(samples, key=lambda s: s.index), monotonic() - started)

    async def status(self) -> dict:
        response = await self.client.get(self.url + "/status")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Gateway /status did not return an object")
        return result

    async def wait_primary(self) -> None:
        started = monotonic()
        while monotonic() - started < self.config.recovery_timeout_seconds:
            sample = await self.request(0, started)
            if sample.backend == "primary" and sample.error is None:
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("Primary did not serve a valid response before the recovery deadline")

    async def observe_recovery(self, result: PhaseResult, restored_at: float) -> None:
        primary_times = [
            (s.started_ms + s.duration_ms) / 1000 for s in result.samples if s.backend == "primary"
        ]
        if primary_times:
            result.recovery_seconds = min(primary_times)
            return
        while monotonic() - restored_at < self.config.recovery_timeout_seconds:
            sample = await self.request(len(result.recovery_probes), restored_at)
            result.recovery_probes.append(sample)
            if sample.backend == "primary" and sample.error is None:
                result.recovery_seconds = (sample.started_ms + sample.duration_ms) / 1000
                return
            await asyncio.sleep(0.25)

    async def phase(self, scenario: Scenario, settings: GatewayConfiguration) -> PhaseResult:
        self.progress(f"Preparing {scenario}: waiting for primary, then warming connections")
        await self.wait_primary()
        if self.config.warmup:
            warmup = await self.load(Scenario.baseline, self.config.warmup)
            if warmup.failed or warmup.primary_requests != self.config.warmup:
                raise RuntimeError("Warmup did not consistently use a healthy primary")

        if scenario == Scenario.baseline:
            self.progress("Measuring baseline")
            result = await self.load(scenario, self.config.requests)
            result.expectations_met = (
                result.primary_requests == result.requests and not result.failed
            )
            return result

        if self.faults is None:
            raise ValueError("Fault scenarios require the compose.benchmark.yaml overlay")
        try:
            if scenario == Scenario.slow_backend:
                await self.faults.delay(self.config.latency_ms)
            else:
                await self.faults.outage()

            if scenario == Scenario.recovery:
                # Open the circuit before removing the fault. These are setup requests.
                for index in range(settings.failure_threshold + 1):
                    sample = await self.request(index, monotonic())
                    if sample.error or sample.backend != "fallback":
                        raise RuntimeError(
                            "Outage setup did not route through fallback; check proxy"
                        )
                    status = await self.status()
                    if status.get("backends", {}).get("primary", {}).get("state") == "open":
                        break
                else:
                    raise RuntimeError("Primary circuit did not open during recovery setup")
                await self.faults.restore()
                restored_at = monotonic()
                self.progress("Measuring recovery from fault removal")
                result = await self.load(scenario, self.config.requests, origin=restored_at)
                self.progress("Observing return to primary (separate recovery probes)")
                await self.observe_recovery(result, restored_at)
                result.expectations_met = (
                    not result.failed
                    and result.recovery_seconds is not None
                    and result.recovery_seconds <= self.config.recovery_timeout_seconds
                )
            else:
                self.progress(f"Measuring {scenario}")
                result = await self.load(scenario, self.config.requests)
                result.expectations_met = (
                    not result.failed and result.fallback_requests == result.requests
                )
            return result
        finally:
            await self.faults.restore()

    async def run(self) -> BenchmarkReport:
        started_at = datetime.now(UTC).isoformat()
        settings = GatewayConfiguration.model_validate((await self.status()).get("configuration"))
        scenarios = (
            [Scenario.baseline, Scenario.outage, Scenario.slow_backend, Scenario.recovery]
            if self.config.scenario == Scenario.all
            else [self.config.scenario]
        )
        if self.config.timeout_seconds <= settings.request_timeout_seconds * 2:
            raise ValueError("Client --timeout must exceed twice the gateway's per-backend timeout")
        if (
            Scenario.slow_backend in scenarios
            and self.config.latency_ms <= settings.request_timeout_seconds * 1000
        ):
            raise ValueError(
                "--latency-ms must exceed the gateway timeout to exercise slow failover"
            )
        if any(s != Scenario.baseline for s in scenarios):
            if self.faults is None:
                raise ValueError("Fault scenarios require the benchmark proxy")
            await self.faults.preflight()
        results = []
        try:
            for scenario in scenarios:
                results.append(await self.phase(scenario, settings))
            if self.faults is not None:
                self.progress("Faults cleared; waiting for normal primary routing")
                await self.wait_primary()
        finally:
            if self.faults is not None:
                await self.faults.restore()
        return BenchmarkReport(
            started_at=started_at,
            config=self.config,
            gateway_configuration=settings,
            phases=results,
            passed=all(result.expectations_met for result in results),
        )


async def run_benchmark(
    config: BenchmarkConfig, progress: Callable[[str], None] = lambda _: None
) -> BenchmarkReport:
    limits = httpx.Limits(
        max_connections=config.concurrency, max_keepalive_connections=config.concurrency
    )
    async with (
        httpx.AsyncClient(timeout=config.timeout_seconds, limits=limits, trust_env=False) as client,
        httpx.AsyncClient(timeout=5, trust_env=False) as control,
    ):
        faults = (
            None
            if config.scenario == Scenario.baseline
            else FaultController(control, str(config.toxiproxy_url))
        )
        return await BenchmarkRunner(config, client, faults, progress).run()
