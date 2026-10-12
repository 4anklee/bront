import json

import httpx
import pytest
from typer.testing import CliRunner

from inference_guard.benchmark import (
    BenchmarkReport,
    GatewayConfiguration,
    Sample,
    Scenario,
    summarize,
)
from inference_guard.cli import app

runner = CliRunner()


def test_help_and_invalid_options():
    assert runner.invoke(app, ["benchmark", "--help"]).exit_code == 0
    for args in (
        ["--requests", "0"],
        ["--concurrency", "0"],
        ["--scenario", "unknown"],
        ["--duration", "0"],
        ["--duration", "-1"],
        ["--duration", "3601"],
        ["--duration", "nan"],
        ["--duration", "inf"],
        ["--duration", "60", "--requests", "100"],
    ):
        result = runner.invoke(app, ["benchmark", *args])
        assert result.exit_code == 2


@pytest.mark.parametrize("workload", [[], ["--requests", "20"], ["--duration", "60"]])
def test_json_export_and_env_configuration(monkeypatch, tmp_path, workload):
    captured = []

    async def fake_run(config, progress):
        captured.append(config)
        progress("Preparing baseline")
        phase = summarize(
            Scenario.baseline,
            [Sample(index=0, started_ms=0, duration_ms=2, backend="primary", status_code=200)],
            0.002,
        )
        phase.expectations_met = True
        return BenchmarkReport(
            started_at="2026-10-12T00:00:00+00:00",
            config=config,
            gateway_configuration=GatewayConfiguration(
                primary_protocol="triton",
                failure_threshold=3,
                request_timeout_seconds=1,
                recovery_timeout_seconds=10,
            ),
            phases=[phase],
            passed=True,
        )

    monkeypatch.setattr("inference_guard.cli.run_benchmark", fake_run)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IG_BENCHMARK_GATEWAY_URL", raising=False)
    monkeypatch.delenv("IG_GATEWAY_PORT", raising=False)
    (tmp_path / ".env").write_text("IG_GATEWAY_PORT=18000\n")
    output = tmp_path / "results" / "run.json"
    result = runner.invoke(app, ["benchmark", *workload, "--json", "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["schema_version"] == 2
    assert report["config"]["requests"] == (
        None if "--duration" in workload else 20 if workload else 100
    )
    assert report["config"]["duration_seconds"] == (60 if "--duration" in workload else None)
    assert report == json.loads(output.read_text())
    assert str(captured[0].gateway_url) == "http://127.0.0.1:18000/"
    assert "Preparing baseline" in result.stderr


def test_unreachable_gateway_has_actionable_error(monkeypatch):
    async def fail(*_):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr("inference_guard.cli.run_benchmark", fail)
    result = runner.invoke(app, ["benchmark"])
    assert result.exit_code == 2
    assert "Connection refused" in result.stderr
