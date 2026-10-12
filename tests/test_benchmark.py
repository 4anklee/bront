import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from inference_guard.benchmark import BenchmarkConfig, BenchmarkRunner, Scenario, percentile
from inference_guard.config import Settings
from inference_guard.faults import FaultController
from inference_guard.gateway import create_app

RESULT = {"model": "double", "outputs": [2, 4, 6]}


def test_percentiles_use_nearest_rank():
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([9, 1, 5], 50) == 5
    assert percentile([4], 99) == 4


async def test_load_bounds_concurrency_and_counts_every_outcome():
    active = peak = calls = 0

    async def handler(_):
        nonlocal active, peak, calls
        index = calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        if index == 0:
            return httpx.Response(503)
        if index == 1:
            return httpx.Response(
                200, json={"model": "double", "outputs": [99], "backend": "primary"}
            )
        if index == 2:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={**RESULT, "backend": "fallback"})

    config = BenchmarkConfig(requests=11, concurrency=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await BenchmarkRunner(config, client).load(Scenario.outage, 11)
    assert peak == 3
    assert calls == result.requests == 11
    assert result.successful == result.fallback_requests == 8
    assert result.failed == 3
    assert result.errors == {"http_503": 1, "invalid_response": 1, "timeout": 1}
    assert [s.index for s in result.samples] == list(range(11))
    assert result.attempted_rps == 11 / result.elapsed_seconds


async def test_total_request_timeout_is_measured_as_failure():
    async def handler(_):
        await asyncio.sleep(1)
        return httpx.Response(200)

    config = BenchmarkConfig(timeout_seconds=0.005)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await BenchmarkRunner(config, client).load(Scenario.baseline, 2)
    assert result.successful == 0
    assert result.errors == {"timeout": 2}


@asynccontextmanager
async def benchmark_stack(scenario=Scenario.all):
    enabled = True
    delay = 0.0
    toxics = []
    calls = []

    async def backend(request):
        calls.append(request.url.host)
        if request.url.host == "primary":
            if not enabled:
                raise httpx.ConnectError("disabled proxy")
            if delay:
                await asyncio.sleep(delay)
        return httpx.Response(200, json=RESULT)

    def control(request):
        nonlocal enabled, delay
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": enabled, "toxics": toxics})
        if request.method == "DELETE":
            toxics.clear()
            delay = 0
            return httpx.Response(204)
        body = json.loads(request.content)
        if request.url.path.endswith("/toxics"):
            toxics.append(body)
            delay = body["attributes"]["latency"] / 1000
        else:
            enabled = body["enabled"]
        return httpx.Response(200, json=body)

    app = create_app(
        Settings(
            _env_file=None,
            primary_url="http://primary",
            fallback_url="http://fallback",
            primary_protocol="mock",
            failure_threshold=1,
            recovery_timeout_seconds=0.02,
            request_timeout_seconds=0.01,
        ),
        httpx.MockTransport(backend),
    )
    config = BenchmarkConfig(
        scenario=scenario,
        requests=8,
        concurrency=2,
        warmup=1,
        latency_ms=30,
        timeout_seconds=0.2,
        recovery_timeout_seconds=1,
    )
    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client,
            httpx.AsyncClient(transport=httpx.MockTransport(control)) as control_client,
        ):
            faults = FaultController(control_client, "http://faults")
            runner = BenchmarkRunner(
                config, client, faults if scenario != Scenario.baseline else None
            )
            yield runner, faults, calls


async def test_all_scenarios_against_gateway_restore_faults_and_report_recovery():
    async with benchmark_stack() as (runner, faults, _):
        report = await runner.run()
        assert report.passed
        assert [p.scenario for p in report.phases] == [
            Scenario.baseline,
            Scenario.outage,
            Scenario.slow_backend,
            Scenario.recovery,
        ]
        assert all(p.requests == 8 and p.successful == 8 for p in report.phases)
        assert report.phases[0].primary_requests == 8
        assert report.phases[1].fallback_requests == 8
        assert report.phases[2].fallback_requests == 8
        recovery = report.phases[3]
        assert recovery.recovery_seconds is not None
        assert 0.02 <= recovery.recovery_seconds < 1
        assert recovery.recovery_probes
        assert len(recovery.samples) == 8  # recovery probes stay outside measured counts
        await faults.preflight()  # enabled, no remaining toxics


async def test_baseline_excludes_preparation_and_warmup_from_counts():
    async with benchmark_stack(Scenario.baseline) as (runner, _, calls):
        report = await runner.run()
    assert len(calls) == 10  # one preparation + one warmup + eight measured
    assert report.phases[0].requests == 8


async def test_cancelled_benchmark_restores_outage(monkeypatch):
    started = asyncio.Event()
    async with benchmark_stack(Scenario.outage) as (runner, faults, _):
        original_load = runner.load

        async def blocked_load(scenario, count, origin=None):
            if scenario == Scenario.outage:
                started.set()
                await asyncio.Event().wait()
            return await original_load(scenario, count, origin)

        monkeypatch.setattr(runner, "load", blocked_load)
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await faults.preflight()


async def test_failed_fault_request_still_triggers_restoration():
    enabled = True

    def handler(request):
        nonlocal enabled
        enabled = json.loads(request.content)["enabled"]
        if not enabled:
            raise httpx.ReadTimeout("Fault applied but response lost")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        faults = FaultController(client, "http://faults")
        with pytest.raises(httpx.ReadTimeout):
            try:
                await faults.outage()
            finally:
                await faults.restore()
        assert enabled


async def test_preexisting_proxy_fault_is_not_modified():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"enabled": False, "toxics": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        faults = FaultController(client, "http://faults")
        with pytest.raises(ValueError, match="no existing faults"):
            await faults.preflight()
        await faults.restore()
    assert methods == ["GET"]
