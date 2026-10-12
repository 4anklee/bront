import json

import httpx
import pytest
from typer.testing import CliRunner

from inference_guard.cli import app
from inference_guard.controls import DEMO_TOXIC, FaultAction, control_fault, inspect_status


def gateway_status():
    return {
        "instance": "gateway-test",
        "backends": {
            name: {"state": "closed", "consecutive_failures": 0} for name in ("primary", "fallback")
        },
        "configuration": {
            "primary_protocol": "mock",
            "failure_threshold": 3,
            "recovery_timeout_seconds": 10,
            "request_timeout_seconds": 1,
        },
    }


@pytest.mark.parametrize("failure", ["unavailable", "wrong-output", "timeout"])
async def test_observed_closed_is_not_health_and_probes_do_not_use_gateway(failure):
    calls = []

    def handle(request):
        calls.append((request.url.host, request.url.path))
        if request.url.path == "/status":
            return httpx.Response(200, json=gateway_status())
        if request.url.path == "/readyz":
            return httpx.Response(200)
        if request.url.host == "primary":
            if failure == "timeout":
                raise httpx.ReadTimeout("test timeout")
            if failure == "wrong-output":
                return httpx.Response(200, json={"model": "double", "outputs": [9, 9, 9]})
            return httpx.Response(503)
        return httpx.Response(200, json={"model": "double", "outputs": [2, 4, 6]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        observed = await inspect_status(client, "http://gateway")
        assert not observed.active_checks
        assert observed.passed  # Readiness only; not a claim that backends work.
        assert len(calls) == 2
        checked = await inspect_status(
            client, "http://gateway", "http://primary", "http://fallback"
        )
    assert checked.backends["primary"].state == "closed"
    assert checked.ready
    assert not checked.passed
    assert not checked.active_checks["primary"].healthy
    assert checked.active_checks["fallback"].healthy
    assert ("primary", "/infer") in calls
    assert ("gateway", "/v1/infer") not in calls


async def test_not_ready_does_not_mean_unhealthy_backend():
    def handle(request):
        return (
            httpx.Response(200, json=gateway_status())
            if request.url.path == "/status"
            else httpx.Response(503)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await inspect_status(client, "http://gateway")
    assert not result.ready
    assert not result.passed
    assert not result.active_checks


async def test_persistent_outage_delay_and_idempotent_reset_preserve_unrelated_toxics():
    enabled = True
    toxics: dict[str, dict] = {}

    def handle(request):
        nonlocal enabled
        if request.method == "GET":
            return httpx.Response(200, json={"enabled": enabled, "toxics": list(toxics.values())})
        if request.method == "DELETE":
            name = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(204 if toxics.pop(name, None) else 404)
        body = json.loads(request.content)
        if request.url.path.endswith("/toxics"):
            toxics[body["name"]] = body
        else:
            enabled = body["enabled"]
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await control_fault(client, "http://proxy", FaultAction.outage)
        assert not enabled
        with pytest.raises(ValueError):
            await control_fault(client, "http://proxy", FaultAction.delay)
        await control_fault(client, "http://proxy", FaultAction.reset)
        assert enabled
        await control_fault(client, "http://proxy", FaultAction.delay, 1500)
        assert toxics[DEMO_TOXIC]["attributes"]["latency"] == 1500
        toxics["someone-else"] = {"name": "someone-else"}
        await control_fault(client, "http://proxy", FaultAction.reset)
        await control_fault(client, "http://proxy", FaultAction.reset)
        assert list(toxics) == ["someone-else"]
        assert enabled
        with pytest.raises(ValueError):
            await control_fault(client, "http://proxy", FaultAction.outage)


def test_status_cli_json_and_exit_codes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def inspect(client, gateway, primary, fallback, timeout_seconds):
        calls.append((gateway, primary, fallback))
        from inference_guard.controls import StatusReport

        return StatusReport(observed_at="now", gateway_url=gateway, ready=False, **gateway_status())

    monkeypatch.setattr("inference_guard.cli.inspect_status", inspect)
    result = CliRunner().invoke(app, ["status", "--gateway-url", "http://example:8000", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["active_checks"] == {}
    assert calls == [("http://example:8000/", None, None)]
    for args in (["--timeout", "nan"], ["--primary-url", "http://primary"]):
        assert CliRunner().invoke(app, ["status", *args]).exit_code == 2


def test_fault_cli_options_and_errors(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def control(client, url, action, latency_ms):
        calls.append((url, action, latency_ms))
        if action == FaultAction.outage:
            raise httpx.ConnectError("control API unavailable")

    monkeypatch.setattr("inference_guard.cli.control_fault", control)
    result = CliRunner().invoke(
        app, ["fault", "delay", "--toxiproxy-url", "http://proxy:8474", "--latency-ms", "1500"]
    )
    assert result.exit_code == 0
    assert calls == [("http://proxy:8474/", FaultAction.delay, 1500)]
    assert "persists" in result.stdout
    result = CliRunner().invoke(app, ["fault", "outage"])
    assert result.exit_code == 2
    assert "fault reset" in result.stderr
    assert CliRunner().invoke(app, ["fault", "delay", "--latency-ms", "0"]).exit_code == 2
