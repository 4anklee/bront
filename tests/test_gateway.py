import asyncio
import json
import struct
import sys
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


TRITON_RESULT = {
    "model_name": "double",
    "model_version": "1",
    "outputs": [
        {"name": "OUTPUT", "datatype": "FP64", "shape": [3], "parameters": {"binary_data_size": 24}}
    ],
}


def triton_response(metadata=None, data=None):
    header = json.dumps(TRITON_RESULT if metadata is None else metadata).encode()
    return httpx.Response(
        200,
        content=header + (struct.pack("<3d", 2, 4, 6) if data is None else data),
        headers={"Inference-Header-Content-Length": str(len(header))},
    )


async def test_triton_protocol_preserves_public_contract():
    def handler(request):
        assert request.url.path == "/v2/models/double/versions/1/infer"
        boundary = int(request.headers["Inference-Header-Content-Length"])
        assert json.loads(request.content[:boundary]) == {
            "inputs": [
                {
                    "name": "INPUT",
                    "datatype": "FP64",
                    "shape": [3],
                    "parameters": {"binary_data_size": 24},
                }
            ],
            "outputs": [{"name": "OUTPUT", "parameters": {"binary_data": True}}],
        }
        assert struct.unpack("<3d", request.content[boundary:]) == (1, 2, 3)
        return triton_response()

    async with gateway(handler, primary_protocol="triton") as (client, _):
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.status_code == 200
        assert response.json() == {**RESULT, "backend": "primary"}


@pytest.mark.parametrize(
    "corruption",
    [
        "model",
        "version",
        "name",
        "datatype",
        "shape",
        "length",
        "missing",
        "size",
        "nonfinite",
        "header",
        "extra_bytes",
    ],
)
async def test_incompatible_triton_response_falls_back(corruption):
    output: dict[str, object] = {
        "name": "OUTPUT",
        "datatype": "FP64",
        "shape": [3],
        "parameters": {"binary_data_size": 24},
    }
    result: dict[str, object] = {"model_name": "double", "model_version": "1", "outputs": [output]}
    data = struct.pack("<3d", 2, 4, 6)
    if corruption == "model":
        result["model_name"] = "wrong-model"
    elif corruption == "version":
        result["model_version"] = "2"
    elif corruption == "name":
        output["name"] = "WRONG"
    elif corruption == "datatype":
        output["datatype"] = "FP32"
    elif corruption == "shape":
        output["shape"] = [1, 3]
    elif corruption == "length":
        data = data[:-1]
    elif corruption == "missing":
        result["outputs"] = []
    elif corruption == "size":
        output["parameters"] = {"binary_data_size": 8}
    elif corruption == "nonfinite":
        data = struct.pack("<3d", float("inf"), 4, 6)
    elif corruption == "extra_bytes":
        data += b"extra"

    def handler(request):
        if request.url.host == "primary":
            response = triton_response(result, data)
            if corruption == "header":
                response.headers["Inference-Header-Content-Length"] = "not-a-number"
            return response
        assert request.url.path == "/infer"
        assert json.loads(request.content) == PAYLOAD
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, primary_protocol="triton") as (client, _):
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.status_code == 200
        assert response.json() == {**RESULT, "backend": "fallback"}


async def test_triton_outage_and_recovery_preserve_results():
    available = False

    def handler(request):
        if request.url.host == "primary":
            if not available:
                raise httpx.ConnectError("Triton stopped")
            return triton_response()
        return httpx.Response(200, json=RESULT)

    async with gateway(handler, primary_protocol="triton", failure_threshold=1) as (client, app):
        breaker = app.state.router.backends[0].breaker
        breaker.clock = lambda: 0.0
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.json() == {**RESULT, "backend": "fallback"}
        available = True
        breaker.clock = lambda: 10.0
        response = await client.post("/v1/infer", json=PAYLOAD)
        assert response.json() == {**RESULT, "backend": "primary"}


async def test_triton_binary_tensors_preserve_numeric_boundaries():
    values = [5e-324, 1e-310, sys.float_info.max / 2]
    expected = [x * 2 for x in values]

    def handler(request):
        boundary = int(request.headers["Inference-Header-Content-Length"])
        assert list(struct.unpack("<3d", request.content[boundary:])) == values
        return triton_response(data=struct.pack("<3d", *expected))

    async with gateway(handler, primary_protocol="triton") as (client, _):
        response = await client.post("/v1/infer", json={"inputs": values})
        assert response.status_code == 200
        assert response.json() == {"model": "double", "outputs": expected, "backend": "primary"}
