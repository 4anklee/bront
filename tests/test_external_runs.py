import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from inference_guard.cli import app
from inference_guard.runs import run_suite
from inference_guard.suites import SuiteError, load_suite


@pytest.fixture
def suite_path(tmp_path):
    path = tmp_path / "suite.toml"
    path.write_text("""version = 1
[target]
url = "http://example.test/private-path?token=secret-query"
[workload]
cases = "cases.jsonl"
requests = 6
concurrency = 2
warmup_requests = 2
request_timeout_seconds = 0.2
""")
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "name": "sum",
                "body": {"features": [1, 2]},
                "expect": {"json": [{"pointer": "/prediction", "equals": 3}]},
            }
        )
        + "\n"
    )
    return path


def change(path, before, after):
    path.write_text(path.read_text().replace(before, after))


async def test_external_contract_concurrency_warmup_and_private_report(suite_path, monkeypatch):
    monkeypatch.setenv("MODEL_TOKEN", "secret-header")
    change(
        suite_path,
        "[workload]",
        """[target.headers]
Authorization = { env = "MODEL_TOKEN", prefix = "Bearer " }
[workload]""",
    )
    loaded = load_suite(suite_path)
    output = suite_path.parent / "report.json"
    active = peak = 0
    observed = []

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        observed.append(request)
        assert request.method == "POST"
        assert request.url.path == "/private-path"
        assert request.headers["authorization"] == "Bearer secret-header"
        assert json.loads(request.content) == {"features": [1, 2]}
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json={"prediction": 3})

    report = await run_suite(loaded, output, httpx.MockTransport(handler))
    assert report.status == "passed"
    assert report.warmup.requests == 2
    assert report.measurement.requests == 6
    assert len(observed) == 8  # No gateway status, health, or configuration preflights.
    assert peak == 2
    assert all(sample.backend is None for sample in report.measurement.samples)
    assert json.loads(output.read_text()) == report.model_dump(mode="json")
    for secret in ("secret-query", "secret-header", "private-path", "features", "prediction"):
        assert secret not in output.read_text()


@pytest.mark.parametrize(
    ("kind", "category", "reason"),
    [
        ("status", "http_status", "unexpected_status"),
        ("output", "validation", "value_mismatch"),
        ("json", "validation", "invalid_json"),
        ("missing", "validation", "missing_path"),
        ("timeout", "timeout", None),
        ("transport", "transport", None),
        ("redirect", "http_status", "unexpected_status"),
    ],
)
async def test_failure_classification(suite_path, kind, category, reason):
    change(suite_path, "warmup_requests = 2", "warmup_requests = 0")

    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("secret-detail")
        if kind == "transport":
            raise httpx.ConnectError("secret-detail")
        return {
            "status": httpx.Response(503, text="secret-detail"),
            "output": httpx.Response(200, json={"prediction": "secret-detail"}),
            "json": httpx.Response(200, content=b"\xff"),
            "missing": httpx.Response(200, json={}),
            "redirect": httpx.Response(302, headers={"Location": "http://elsewhere.test"}),
        }[kind]

    report = await run_suite(
        load_suite(suite_path), suite_path.parent / "r.json", httpx.MockTransport(handler)
    )
    assert report.status == "failed"
    assert report.measurement.errors == {category: 6}
    assert all(sample.reason == reason for sample in report.measurement.samples)
    assert "secret-detail" not in report.model_dump_json()


async def test_whole_request_timeout(suite_path):
    change(suite_path, "request_timeout_seconds = 0.2", "request_timeout_seconds = 0.01")

    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    report = await asyncio.wait_for(
        run_suite(
            load_suite(suite_path), suite_path.parent / "r.json", httpx.MockTransport(handler)
        ),
        1,
    )
    assert report.status == "failed"
    assert report.reason == "warmup_failed"
    assert report.warmup.errors == {"timeout": 2}
    assert report.measurement.requests == 0


async def test_cancel_keeps_completed_and_inflight_samples(suite_path):
    change(suite_path, "warmup_requests = 2", "warmup_requests = 0")
    inflight = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"prediction": 3})
        if calls == 3:
            inflight.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    output = suite_path.parent / "r.json"
    task = asyncio.create_task(
        run_suite(load_suite(suite_path), output, httpx.MockTransport(handler))
    )
    await asyncio.wait_for(inflight.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads(output.read_text())
    assert report["status"] == "cancelled"
    assert report["finished_at"] is not None
    assert report["measurement"]["successful"] == 1
    assert report["measurement"]["errors"] == {"cancelled": 2}
    assert report["measurement"]["requests"] == 3


@pytest.mark.parametrize("duration", [False, True])
async def test_cases_cycle_and_duration(suite_path, duration):
    change(suite_path, "warmup_requests = 2", "warmup_requests = 0")
    if duration:
        change(suite_path, "requests = 6", "duration_seconds = 0.01")
    case_path = suite_path.parent / "cases.jsonl"
    with case_path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "name": "second",
                    "body": {"features": [4, 5]},
                    "expect": {"json": [{"pointer": "/prediction", "equals": 9}]},
                }
            )
        )

    async def handler(request):
        await asyncio.sleep(0.001)
        return httpx.Response(
            200, json={"prediction": sum(json.loads(request.content)["features"])}
        )

    report = await run_suite(
        load_suite(suite_path), suite_path.parent / "r.json", httpx.MockTransport(handler)
    )
    assert report.status == "passed"
    assert report.measurement.requests >= 2
    for sample in report.measurement.samples:
        assert sample.case == ("sum" if sample.index % 2 == 0 else "second")
    if duration:
        assert report.measurement.elapsed_seconds >= 0.01
    else:
        assert report.measurement.requests == 6


async def test_duration_sample_cap_fails_instead_of_truncating_silently(suite_path):
    change(suite_path, "requests = 6", "duration_seconds = 60\nmax_samples = 3")
    report = await run_suite(
        load_suite(suite_path),
        suite_path.parent / "r.json",
        httpx.MockTransport(lambda _: httpx.Response(200, json={"prediction": 3})),
    )
    assert report.status == "error"
    assert report.reason == "sample_limit_reached"
    assert report.measurement.requests == 3


async def test_internal_errors_cannot_count_as_success(suite_path):
    def handler(request):
        raise RuntimeError("secret-error")

    report = await run_suite(
        load_suite(suite_path), suite_path.parent / "r.json", httpx.MockTransport(handler)
    )
    assert report.status == "error"
    assert report.warmup.successful == 0
    assert "execution" in report.warmup.errors
    assert "secret-error" not in report.model_dump_json()


async def test_assertion_thresholds(suite_path):
    change(suite_path, "warmup_requests = 2", "warmup_requests = 0")
    with suite_path.open("a") as stream:
        stream.write("\n[assertions]\nmax_failed_requests = 6\nmax_p95_ms = 0.00000001\n")
    report = await run_suite(
        load_suite(suite_path),
        suite_path.parent / "r.json",
        httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    assert report.assertions[0].passed
    assert not report.assertions[1].passed
    assert report.status == "failed"


@pytest.mark.parametrize("setting", ['pointer = "/backend"', 'header = "X-Backend"'])
async def test_optional_backend_identity(suite_path, setting):
    change(suite_path, "[workload]", f"[target.backend]\n{setting}\n[workload]")
    for identity in (None, "worker-a"):
        report = await run_suite(
            load_suite(suite_path),
            suite_path.parent / "r.json",
            httpx.MockTransport(
                lambda _, identity=identity: httpx.Response(
                    200,
                    json={"prediction": 3, "backend": identity},
                    headers={"X-Backend": identity} if identity else {},
                )
            ),
        )
        assert report.status == "passed"
        assert all(sample.backend == identity for sample in report.measurement.samples)


async def test_output_cannot_overwrite_inputs(suite_path):
    loaded = load_suite(suite_path)
    for output in (suite_path, suite_path.parent / "cases.jsonl"):
        before = output.read_bytes()
        with pytest.raises(SuiteError):
            await run_suite(loaded, output)
        assert output.read_bytes() == before


def test_relative_cases_and_url_override(suite_path, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_suite(suite_path, url="http://localhost:9999/infer")
    assert loaded.cases[0].name == "sum"
    assert str(loaded.suite.target.url) == "http://localhost:9999/infer"
    assert len(loaded.cases_sha256) == 64


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("requests = 6", "requests = 6\nduration_seconds = 10"),
        ("requests = 6", "requests = 6\nmax_samples = 2"),
        ("concurrency = 2", "concurrency = 0"),
        ("requests = 6", "duration_seconds = nan"),
        ("[workload]", '[target.headers]\nAuthorization = {env = "MISSING_MODEL_KEY"}\n[workload]'),
        ("[workload]", '[target.headers]\nHost = "secret-header"\n[workload]'),
        ("[workload]", '[target.headers]\nX = "secret-header\\r\\n"\n[workload]'),
        (
            "http://example.test/private-path?token=secret-query",
            "http://user:secret-password@example.test",
        ),
    ],
)
def test_invalid_config_is_sanitized(suite_path, before, after, monkeypatch):
    monkeypatch.delenv("MISSING_MODEL_KEY", raising=False)
    change(suite_path, before, after)
    result = CliRunner().invoke(app, ["run", str(suite_path)])
    assert result.exit_code == 2
    assert "Configuration error" in result.output
    assert "secret-" not in result.output


def test_header_whitespace_is_rejected_before_traffic(suite_path):
    change(suite_path, "[workload]", '[target.headers]\nX = " secret-value "\n[workload]')
    with pytest.raises(SuiteError, match="whitespace"):
        load_suite(suite_path)


async def test_unwritable_report_sends_no_requests(suite_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(OSError):
        await run_suite(load_suite(suite_path), suite_path.parent, httpx.MockTransport(handler))
    assert calls == 0


async def test_status_only_get_omits_body_and_accepts_non_json(suite_path):
    change(suite_path, "[target]", '[target]\nmethod = "GET"')
    (suite_path.parent / "cases.jsonl").write_text('{"name":"available","expect":{"status":204}}')

    def handler(request):
        assert request.method == "GET"
        assert request.content == b""
        assert "content-type" not in request.headers
        return httpx.Response(204)

    report = await run_suite(
        load_suite(suite_path), suite_path.parent / "r.json", httpx.MockTransport(handler)
    )
    assert report.status == "passed"


def test_invalid_case_preflight_and_unique_names(suite_path):
    cases = suite_path.parent / "cases.jsonl"
    original = cases.read_text()
    for content in (original + original, '{"body":NaN}', '{"name":"secret-name","body":[]}'):
        cases.write_text(content)
        with pytest.raises(SuiteError) as exc:
            load_suite(suite_path)
        assert "secret-name" not in str(exc.value)


@pytest.mark.parametrize(("status", "exit_code"), [(200, 0), (503, 1)])
def test_cli_json_and_exit_codes(suite_path, monkeypatch, status, exit_code):
    async def execute(loaded, output):
        return await run_suite(
            loaded,
            output,
            httpx.MockTransport(lambda _: httpx.Response(status, json={"prediction": 3})),
        )

    monkeypatch.setattr("inference_guard.run_cli.run_suite", execute)
    output = suite_path.parent / "r.json"
    result = CliRunner().invoke(app, ["run", str(suite_path), "--output", str(output), "--json"])
    assert result.exit_code == exit_code
    assert json.loads(result.output) == json.loads(output.read_text())
    assert json.loads(result.output)["status"] == ("passed" if exit_code == 0 else "failed")
