import asyncio
import json

import httpx
import pytest

from bront.artifacts import write_artifact
from bront.comparison_models import ComparisonPolicy
from bront.comparisons import compare_files
from bront.evidence import ReadBudget, load_evidence, validate_run
from bront.provenance import digest
from bront.runs import run_suite
from bront.series import run_series
from bront.suites import SuiteError, load_suite


@pytest.fixture
def loaded(tmp_path):
    path = tmp_path / "suite.toml"
    path.write_text("""version = 1
[target]
url = "http://predictor/predict"
[workload]
cases = "cases.jsonl"
requests = 6
concurrency = 2
warmup_requests = 1
[metadata]
model_revision = "sum-v1"
serving_revision = "serving-v1"
architecture = "x86_64"
[metadata.resources]
cpu = "2"
memory = "2Gi"
""")
    (tmp_path / "cases.jsonl").write_text(
        '{"name":"known","body":{},"expect":{"json":[{"pointer":"/value","equals":42}]}}'
    )
    return load_suite(path)


def install_runner(monkeypatch, handler):
    async def execute(loaded, output):
        return await run_suite(loaded, output, httpx.MockTransport(handler))

    monkeypatch.setattr("bront.series.run_suite", execute)


async def test_real_reports_are_consistent_and_series_can_be_compared(
    loaded, tmp_path, monkeypatch
):
    install_runner(monkeypatch, lambda _: httpx.Response(200, json={"value": 42}))
    left, right = tmp_path / "base.json", tmp_path / "candidate.json"
    baseline = await run_series(loaded, left, 3)
    candidate = await run_series(loaded, right, 3)
    assert baseline.status == candidate.status == "passed"
    assert len({r.run_id for r in baseline.runs + candidate.runs}) == 6
    evidence = load_evidence([left], "baseline", ReadBudget())
    assert not evidence.issues
    assert len(evidence.runs) == 3
    report = compare_files([left], [right], ComparisonPolicy(version=1, minimum_requests=6))
    assert report.status == "passed", report.issues
    assert report.metrics["p95_ms"].baseline.minimum > 0
    with pytest.raises(SuiteError):
        compare_files(
            [left], [right], ComparisonPolicy(version=1), output=tmp_path / baseline.runs[0].path
        )


async def test_completed_failures_are_retained_across_repeats(loaded, tmp_path, monkeypatch):
    loaded.suite.workload.warmup_requests = 0
    install_runner(monkeypatch, lambda _: httpx.Response(200, json={"value": 999}))
    manifest = await run_series(loaded, tmp_path / "series.json", 3)
    assert manifest.status == "failed" and len(manifest.runs) == 3
    for reference in manifest.runs:
        report = validate_run(json.loads((tmp_path / reference.path).read_text()))
        assert report.status == "failed"
        assert report.measurement.errors == {"validation": 6}


async def test_series_stops_on_execution_error(loaded, tmp_path, monkeypatch):
    def handler(request):
        raise RuntimeError("secret-exception")

    install_runner(monkeypatch, handler)
    manifest = await run_series(loaded, tmp_path / "series.json", 3)
    assert manifest.status == "error"
    assert len(manifest.runs) == 1
    assert "secret-exception" not in (tmp_path / "series.json").read_text()
    assert load_evidence([tmp_path / "series.json"], "baseline", ReadBudget()).issues


async def test_cancelled_series_keeps_the_interrupted_child(loaded, tmp_path, monkeypatch):
    pending = asyncio.Event()

    async def handler(request):
        pending.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    install_runner(monkeypatch, handler)
    output = tmp_path / "series.json"
    task = asyncio.create_task(run_series(loaded, output, 3))
    await asyncio.wait_for(pending.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    manifest = json.loads(output.read_text())
    assert manifest["status"] == "cancelled"
    assert len(manifest["runs"]) == 1
    child = json.loads((tmp_path / manifest["runs"][0]["path"]).read_text())
    assert child["status"] == "cancelled"
    assert manifest["runs"][0]["sha256"] == digest(child)


async def test_manifest_cannot_overwrite_suite_or_cases(loaded):
    for path in (loaded.source, loaded.cases_path):
        before = path.read_bytes()
        with pytest.raises(SuiteError):
            await run_series(loaded, path, 3)
        assert path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["digest", "escape", "identity", "incomplete"])
async def test_untrustworthy_manifests_are_rejected(loaded, tmp_path, monkeypatch, tamper):
    install_runner(monkeypatch, lambda _: httpx.Response(200, json={"value": 42}))
    output = tmp_path / "series.json"
    manifest = await run_series(loaded, output, 3)
    if tamper == "digest":
        manifest.runs[0].sha256 = "a" * 64
    elif tamper == "escape":
        manifest.runs[0].path = "../private.json"
    elif tamper == "identity":
        manifest.runs[0].run_id = "different-run"
    else:
        manifest.status = "cancelled"
    write_artifact(manifest, output)
    evidence = load_evidence([output], "baseline", ReadBudget())
    assert evidence.issues


async def test_validation_budget_remains_separate_from_request_failure_budget(loaded, tmp_path):
    loaded.suite.workload.warmup_requests = 0
    loaded.suite.assertions.max_failed_requests = 6
    report = await run_suite(
        loaded,
        tmp_path / "run.json",
        httpx.MockTransport(lambda _: httpx.Response(200, json={"value": 999})),
    )
    assert report.assertions[0].passed
    guard = next(a for a in report.assertions if a.name == "max_validation_failures")
    assert not guard.passed and guard.actual == 6
    assert report.status == "failed"


async def test_p99_and_error_rate_are_explicit_release_checks(loaded, tmp_path):
    loaded.suite.workload.warmup_requests = 0
    loaded.suite.assertions.max_failed_requests = 6
    loaded.suite.assertions.max_p99_ms = 0.00000001
    loaded.suite.assertions.max_error_rate = 0.25
    report = await run_suite(
        loaded, tmp_path / "run.json", httpx.MockTransport(lambda _: httpx.Response(503))
    )
    checks = {a.name: a for a in report.assertions}
    assert not checks["max_p99_ms"].passed
    assert checks["max_error_rate"].actual == 1
    assert not checks["max_error_rate"].passed


async def test_credentials_and_host_changes_do_not_change_contract(loaded, tmp_path):
    from pydantic import HttpUrl

    from bront.runs import make_report

    loaded.suite.target.headers = {"Authorization": "secret-token-one"}
    first = make_report(loaded)
    loaded.suite.target.headers = {"Authorization": "secret-token-two"}
    loaded.suite.target.url = HttpUrl("https://candidate:9999/predict")
    second = make_report(loaded)
    assert first.provenance.contract_sha256 == second.provenance.contract_sha256
    assert "secret-token" not in first.model_dump_json() + second.model_dump_json()
    loaded.suite.target.url = HttpUrl("https://candidate:9999/other-route")
    assert make_report(loaded).provenance.contract_sha256 != first.provenance.contract_sha256
