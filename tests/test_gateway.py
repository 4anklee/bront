import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from inference_guard.config import Settings
from inference_guard.gateway import create_app

PAYLOAD = {"model": "double", "inputs": [1, 2, 3]}
RESULT = {"model": "double", "outputs": [2, 4, 6]}


@asynccontextmanager
async def gateway(handler, **overrides):
    settings = Settings(
        _env_file=None,
        primary_url="http://primary",
        fallback_url="http://fallback",
        **overrides,
    )
    app = create_app(settings, httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            yield client, app


async def test_primary_success_and_metrics():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=RESULT)

    async with gateway(handler) as (client, _):
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.status_code == 200
        assert response.json() == {**RESULT, "backend": "primary"}
        assert calls == ["primary"]
        metrics = (await client.get("/metrics")).text
        assert 'inferenceguard_requests_total{outcome="success"} 1.0' in metrics


@pytest.mark.parametrize("failure", ["http", "timeout", "connection", "json", "shape"])
async def test_fallback_and_circuit_skips_failed_primary(failure):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "primary":
            if failure == "timeout":
                raise httpx.ReadTimeout("slow")
            if failure == "connection":
                raise httpx.ConnectError("offline")
            if failure == "json":
                return httpx.Response(200, text="not json")
            if failure == "shape":
                return httpx.Response(200, json={"model": "double", "outputs": [2]})
            return httpx.Response(503)
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, failure_threshold=1) as (client, _):
        for _ in range(2):
            response = await client.post("/v1/infer", json=PAYLOAD)
            assert response.status_code == 200
            assert response.json() == {**RESULT, "backend": "fallback"}
        assert calls == ["primary", "fallback", "fallback"]
        status = (await client.get("/status")).json()
        assert status["backends"]["primary"]["state"] == "open"


async def test_total_attempt_deadline():
    async def handler(request):
        if request.url.host == "primary":
            await asyncio.sleep(1)
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, request_timeout_seconds=0.01) as (client, _):
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.json()["backend"] == "fallback"


async def test_all_backends_down_returns_503():
    async with gateway(lambda _: httpx.Response(503)) as (client, _):
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.status_code == 503
        assert response.json() == {"detail": "No inference backend available"}


async def test_recovered_primary_is_used_after_cooldown():
    broken = True

    def handler(request):
        if request.url.host == "primary" and broken:
            return httpx.Response(503)
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, failure_threshold=1) as (client, app):
        breaker = app.state.router.backends[0].breaker
        now = [0.0]
        breaker.clock = lambda: now[0]
        assert (await client.post("/v1/infer", json=PAYLOAD)).json()["backend"] == "fallback"
        broken = False
        now[0] = 10.0
        assert (await client.post("/v1/infer", json=PAYLOAD)).json()["backend"] == "primary"
        assert breaker.state == "closed"


async def test_invalid_input_never_reaches_backend():
    def handler(_):
        pytest.fail("Invalid input must not reach backend")

    async with gateway(handler) as (client, _):
        for payload in ({"inputs": []}, {"model": "unknown", "inputs": [1]}):
            assert (await client.post("/v1/infer", json=payload)).status_code == 422


async def test_concurrent_requests_use_fallback_while_recovery_probe_runs():
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def handler(request):
        if request.url.host == "primary":
            probe_started.set()
            await release_probe.wait()
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, failure_threshold=1) as (client, app):
        breaker = app.state.router.backends[0].breaker
        breaker.clock = lambda: 0.0
        ticket = breaker.acquire()
        assert ticket is not None
        breaker.fail(ticket)
        breaker.clock = lambda: 10.0
        probe = asyncio.create_task(client.post("/v1/infer", json=PAYLOAD))
        try:
            await asyncio.wait_for(probe_started.wait(), timeout=1)
            responses = await asyncio.gather(
                *(client.post("/v1/infer", json=PAYLOAD) for _ in range(5))
            )
            assert all(response.json()["backend"] == "fallback" for response in responses)
        finally:
            release_probe.set()
            response = await probe
        assert response.json()["backend"] == "primary"
        assert breaker.state == "closed"


async def test_cancelled_recovery_probe_does_not_leave_circuit_half_open():
    probe_started = asyncio.Event()

    async def handler(_):
        probe_started.set()
        await asyncio.Event().wait()

    async with gateway(handler, failure_threshold=1) as (client, app):
        breaker = app.state.router.backends[0].breaker
        breaker.clock = lambda: 0.0
        ticket = breaker.acquire()
        assert ticket is not None
        breaker.fail(ticket)
        breaker.clock = lambda: 10.0
        probe = asyncio.create_task(client.post("/v1/infer", json=PAYLOAD))
        try:
            await asyncio.wait_for(probe_started.wait(), timeout=1)
        finally:
            probe.cancel()
            with pytest.raises(asyncio.CancelledError):
                await probe
        assert breaker.state == "open"
        breaker.clock = lambda: 20.0
        assert breaker.acquire() is not None
