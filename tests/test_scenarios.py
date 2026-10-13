import asyncio
import json

import httpx
import pytest

from inference_guard.evidence import validate_run
from inference_guard.runs import run_suite
from inference_guard.suites import SuiteError, load_suite


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "suite.toml"
    path.write_text("""version = 1
[target]
url = "http://model/predict"
[workload]
cases = "cases.jsonl"
duration_seconds = 0.1
concurrency = 2
request_timeout_seconds = 0.1
[scenario]
kind = "proxy-outage"
url = "http://control"
proxy = "custom-model"
at_seconds = 0.02
hold_seconds = 0.02
control_timeout_seconds = 0.1
observe_timeout_seconds = 0.1
""")
    (tmp_path / "cases.jsonl").write_text(
        '{"name":"prediction","body":{},"expect":{"json":[{"pointer":"/value","equals":42}]}}'
    )
    return path


class Proxy:
    def __init__(self):
        self.state: dict = {
            "name": "custom-model",
            "listen": ":8666",
            "upstream": "model:9000",
            "enabled": True,
            "toxics": [],
        }
        self.calls = []
        self.injected = asyncio.Event()
        self.fail_apply = False
        self.fail_restore = False
        self.pause_apply = False

    async def control(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.url.path.startswith("/proxies/custom-model")
        if request.method == "GET":
            return httpx.Response(200, json=self.state)
        if request.method == "DELETE":
            self.state["toxics"] = [
                t for t in self.state["toxics"] if t["name"] != request.url.path.split("/")[-1]
            ]
            return httpx.Response(204)
        body = json.loads(request.content)
        if self.fail_restore and body.get("enabled"):
            raise httpx.ConnectError("secret-control-output")
        if "enabled" in body:
            self.state["enabled"] = body["enabled"]
        else:
            self.state["toxics"].append(body)
        if body.get("enabled") is False or "type" in body:
            self.injected.set()
            if self.fail_apply:
                raise httpx.ReadTimeout("secret-control-output")
            if self.pause_apply:
                await asyncio.Event().wait()
        return httpx.Response(200, json=self.state)

    async def inference(self, request):
        assert request.url.path == "/predict"  # No demo status/health contract.
        await asyncio.sleep(0.001)
        return httpx.Response(
            200, json={"value": 42, "backend": "primary" if self.state["enabled"] else "fallback"}
        )

    async def run(self, path):
        return await run_suite(
            load_suite(path),
            path.parent / "report.json",
            httpx.MockTransport(self.inference),
            httpx.MockTransport(self.control),
        )


def change(path, old, new):
    path.write_text(path.read_text().replace(old, new))


@pytest.mark.parametrize("delay", [False, True])
async def test_generic_fault_timeline_restoration_and_traffic(path, delay):
    if delay:
        change(path, 'kind = "proxy-outage"', 'kind = "proxy-delay"\nlatency_ms = 80')
    proxy = Proxy()
    report = await proxy.run(path)
    assert report.status == "passed"
    assert validate_run(report.model_dump(mode="json")) == report
    evidence = report.scenario
    assert evidence is not None and evidence.completed
    assert evidence.cleanup == "restored"
    assert [e.action for e in evidence.events] == [
        "preflight",
        "apply",
        "observed",
        "restore",
        "restored",
    ]
    assert all(e.status == "completed" for e in evidence.events)
    assert evidence.events[0].started_ms < 0
    assert evidence.events[1].scheduled_ms == 20
    assert evidence.events[1].started_ms >= 20
    assert evidence.recovery is not None
    assert evidence.recovery.first_valid_response_ms is not None
    assert not evidence.recovery.backend_identity_available
    assert proxy.state["enabled"] and not proxy.state["toxics"]
    assert "control" not in str(evidence.configuration.get("url", ""))


async def test_lost_apply_acknowledgement_still_restores(path):
    proxy = Proxy()
    proxy.fail_apply = True
    report = await proxy.run(path)
    assert report.status == "error"
    assert report.scenario is not None and report.scenario.cleanup == "restored"
    assert proxy.state["enabled"]
    assert "secret-control-output" not in report.model_dump_json()


async def test_cancellation_during_mutation_restores_and_saves(path):
    proxy = Proxy()
    proxy.pause_apply = True
    task = asyncio.create_task(proxy.run(path))
    await asyncio.wait_for(proxy.injected.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((path.parent / "report.json").read_text())
    assert report["status"] == "cancelled"
    assert report["scenario"]["cleanup"] == "restored"
    assert proxy.state["enabled"]
    assert any(e["status"] == "cancelled" for e in report["scenario"]["events"])


async def test_cleanup_failure_is_execution_error_even_if_all_outputs_correct(path):
    proxy = Proxy()
    proxy.fail_restore = True
    report = await proxy.run(path)
    assert report.status == "error"
    assert report.reason == "scenario_cleanup_failed"
    assert report.scenario is not None and report.scenario.cleanup == "failed"
    assert not report.scenario.completed
    assert "secret-control-output" not in report.model_dump_json()


async def test_bad_preflight_never_mutates_or_sends_traffic(path):
    proxy = Proxy()
    proxy.state["enabled"] = False
    report = await proxy.run(path)
    assert report.status == "error"
    assert report.measurement.requests == 0
    assert all(method == "GET" for method, _ in proxy.calls)
    assert report.scenario is not None and report.scenario.cleanup == "not_required"


async def test_short_or_capped_traffic_cannot_pass_unperformed_scenario(path):
    change(path, "duration_seconds = 0.1", "duration_seconds = 0.1\nmax_samples = 1")
    proxy = Proxy()
    report = await proxy.run(path)
    assert report.status == "error"
    assert report.reason == "execution_error:traffic_ended_before_scenario_completed"
    assert not any(method == "POST" for method, _ in proxy.calls)


async def test_slow_apply_causing_traffic_end_restores(path):
    proxy = Proxy()
    proxy.pause_apply = True
    report = await proxy.run(path)
    assert report.status == "error"
    assert proxy.state["enabled"]
    assert report.scenario is not None and report.scenario.cleanup == "restored"


async def test_proxy_topology_change_prevents_wrong_restore(path):
    proxy = Proxy()
    task = asyncio.create_task(proxy.run(path))
    await proxy.injected.wait()
    proxy.state["upstream"] = "different-service:9000"
    report = await task
    assert report.status == "error"
    assert report.scenario is not None and report.scenario.cleanup == "failed"
    assert not proxy.state["enabled"]  # Refused to alter the replacement topology.


async def test_foreign_toxic_is_preserved_and_run_fails(path):
    change(path, 'kind = "proxy-outage"', 'kind = "proxy-delay"\nlatency_ms = 80')
    proxy = Proxy()
    task = asyncio.create_task(proxy.run(path))
    await proxy.injected.wait()
    proxy.state["toxics"].append({"name": "someone-else", "type": "latency"})
    report = await task
    assert report.status == "error"
    assert [t["name"] for t in proxy.state["toxics"]] == ["someone-else"]


@pytest.mark.parametrize("preferred", [True, False])
async def test_service_and_preferred_backend_recovery_are_distinct(path, preferred, monkeypatch):
    change(path, "[workload]", '[target.backend]\npointer = "/backend"\n[workload]')
    with path.open("a") as stream:
        stream.write(
            "\n[assertions]\nrecovery_within_seconds = 0.05\n"
            'preferred_backend = "primary"\npreferred_backend_within_seconds = 0.05\n'
        )
    proxy = Proxy()
    if not preferred:

        async def fallback_only(request):
            await asyncio.sleep(0.001)
            return httpx.Response(200, json={"value": 42, "backend": "fallback"})

        monkeypatch.setattr(proxy, "inference", fallback_only)
    report = await proxy.run(path)
    assert report.status == ("passed" if preferred else "failed")
    assert report.assertions[-2].passed
    assert report.assertions[-1].passed is preferred
    if not preferred:
        assert report.assertions[-1].actual is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("duration_seconds = 0.1", "requests = 10"),
        ("at_seconds = 0.02", "at_seconds = 0.2"),
        ("hold_seconds = 0.02", "hold_seconds = 0.2"),
        ('proxy = "custom-model"', 'proxy = "../other"'),
        ('proxy = "custom-model"', 'proxy = ".."'),
        ('url = "http://control"', 'url = "http://secret:password@control"'),
    ],
)
def test_invalid_capabilities_rejected_locally(path, old, new):
    change(path, old, new)
    with pytest.raises(SuiteError):
        load_suite(path)


def test_preferred_backend_requires_identity_before_mutation(path):
    with path.open("a") as stream:
        stream.write(
            '\n[assertions]\npreferred_backend = "primary"\npreferred_backend_within_seconds = 1\n'
        )
    with pytest.raises(SuiteError):
        load_suite(path)


async def test_unobserved_backend_identity_aborts_before_mutation(path, monkeypatch):
    change(path, "[workload]", '[target.backend]\npointer = "/missing-backend"\n[workload]')
    with path.open("a") as stream:
        stream.write(
            '\n[assertions]\npreferred_backend = "primary"\npreferred_backend_within_seconds = 1\n'
        )
    proxy = Proxy()
    report = await proxy.run(path)
    assert report.status == "error"
    assert report.reason == "execution_error:backend_identity_not_observed_before_action"
    assert not any(method == "POST" for method, _ in proxy.calls)


async def test_interrupt_during_final_cleanup_waits_for_restoration(path, monkeypatch):
    proxy = Proxy()
    proxy.fail_apply = True
    restoring = asyncio.Event()
    release = asyncio.Event()
    original = proxy.control

    async def control(request):
        if request.method == "POST" and json.loads(request.content).get("enabled"):
            restoring.set()
            await release.wait()
        return await original(request)

    monkeypatch.setattr(proxy, "control", control)
    task = asyncio.create_task(proxy.run(path))
    await asyncio.wait_for(restoring.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((path.parent / "report.json").read_text())
    assert report["status"] != "passed"
    assert report["scenario"]["cleanup"] == "restored"
    assert proxy.state["enabled"]


async def test_control_close_failure_still_saves_report(path, monkeypatch):
    from inference_guard.scenarios import ScenarioSession

    original = ScenarioSession.close

    async def close(self):
        await original(self)
        raise RuntimeError("secret-close-error")

    monkeypatch.setattr(ScenarioSession, "close", close)
    report = await Proxy().run(path)
    assert report.status == "error"
    assert report.reason == "scenario_control_close_failed"
    assert "secret-close-error" not in (path.parent / "report.json").read_text()
