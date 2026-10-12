"""InferenceGuard command-line tools."""

import asyncio
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from inference_guard.benchmark import BenchmarkConfig, BenchmarkReport, Scenario, run_benchmark

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


@app.callback()
def main() -> None:
    """Measure inference reliability under concurrent traffic."""


def render(report: BenchmarkReport) -> None:
    console = Console()
    table = Table(title=f"InferenceGuard — {report.gateway_configuration.primary_protocol} primary")
    for title in (
        "Scenario",
        "OK/Total",
        "Primary",
        "Fallback",
        "Req/s",
        "p50 ms",
        "p95 ms",
        "p99 ms",
        "Check",
    ):
        table.add_column(title)
    for phase in report.phases:
        table.add_row(
            phase.scenario.value,
            f"{phase.successful}/{phase.requests}",
            str(phase.primary_requests),
            str(phase.fallback_requests),
            f"{phase.attempted_rps:.1f}",
            f"{phase.latency_ms['p50']:.2f}",
            f"{phase.latency_ms['p95']:.2f}",
            f"{phase.latency_ms['p99']:.2f}",
            "PASS" if phase.expectations_met else "FAIL",
        )
    console.print(table)
    console.print("Latency includes failed requests. Warmup and recovery probes are excluded.")
    for phase in report.phases:
        console.print(f"{phase.scenario}: success {phase.success_rate:.1%}; errors {phase.errors}")
        if report.config.duration_seconds is not None:
            console.print(
                f"Measured {phase.requests} requests over {phase.elapsed_seconds:.3f}s "
                f"including drain; requested duration {report.config.duration_seconds:g}s"
            )
        if phase.scenario == Scenario.recovery:
            recovery = (
                f"{phase.recovery_seconds:.3f}s"
                if phase.recovery_seconds is not None
                else "not observed before deadline"
            )
            console.print(
                f"Recovery after fault removal: {recovery}; "
                f"{len(phase.recovery_probes)} extra probes"
            )


@app.command()
def benchmark(
    scenario: Annotated[
        Scenario, typer.Option(help="Scenario to measure; all runs four phases.")
    ] = Scenario.baseline,
    requests: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=1_000_000,
            help="Measured requests per scenario; defaults to 100 unless --duration is set.",
        ),
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option(
            min=0.001,
            max=3600,
            help="Seconds of measured traffic per scenario; mutually exclusive with --requests.",
        ),
    ] = None,
    concurrency: Annotated[
        int, typer.Option(min=1, max=1000, help="Maximum in-flight requests.")
    ] = 10,
    warmup: Annotated[
        int, typer.Option(min=0, max=10000, help="Unmeasured warmup requests per phase.")
    ] = 10,
    gateway_url: Annotated[
        str | None,
        typer.Option(
            help="Gateway URL; otherwise IG_BENCHMARK_GATEWAY_URL or local IG_GATEWAY_PORT."
        ),
    ] = None,
    toxiproxy_url: Annotated[
        str | None,
        typer.Option(
            help="Fault-control URL; defaults to IG_BENCHMARK_TOXIPROXY_URL or IG_TOXIPROXY_PORT."
        ),
    ] = None,
    latency_ms: Annotated[
        int, typer.Option(min=1, max=30000, help="Injected response delay for slow-backend.")
    ] = 2000,
    timeout: Annotated[
        float, typer.Option(min=0.001, max=300, help="Total client timeout per request, seconds.")
    ] = 5,
    recovery_timeout: Annotated[
        float, typer.Option(min=0.001, max=300, help="Deadline for primary recovery, seconds.")
    ] = 30,
    output: Annotated[
        Path | None, typer.Option(help="Write full JSON report, including individual samples.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON on stdout; progress stays on stderr.")
    ] = False,
) -> None:
    """Benchmark baseline, outage, slow-backend, or recovery.

    Fault scenarios require the compose.benchmark.yaml overlay and modify its
    primary proxy for the duration of the run. Use a local demo stack and run
    one benchmark at a time. Faults are restored on normal exit and interruption.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        config = BenchmarkConfig(
            gateway_url=gateway_url
            or os.getenv("IG_BENCHMARK_GATEWAY_URL")
            or f"http://127.0.0.1:{os.getenv('IG_GATEWAY_PORT', '8000')}",
            toxiproxy_url=toxiproxy_url
            or os.getenv("IG_BENCHMARK_TOXIPROXY_URL")
            or f"http://127.0.0.1:{os.getenv('IG_TOXIPROXY_PORT', '18474')}",
            scenario=scenario,
            requests=requests,
            duration_seconds=duration,
            concurrency=concurrency,
            warmup=warmup,
            timeout_seconds=timeout,
            latency_ms=latency_ms,
            recovery_timeout_seconds=recovery_timeout,
        )
        report = asyncio.run(run_benchmark(config, lambda message: typer.echo(message, err=True)))
        serialized = report.model_dump_json(indent=2) + "\n"
        if json_output:
            typer.echo(serialized, nl=False)
        else:
            render(report)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialized)
            typer.echo(f"Report saved to {output}", err=True)
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
        typer.echo(f"Benchmark could not complete: {exc}", err=True)
        if scenario != Scenario.baseline:
            typer.echo(
                "Check the gateway and compose.benchmark.yaml fault-proxy service.", err=True
            )
        raise typer.Exit(2) from exc
    if not report.passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
