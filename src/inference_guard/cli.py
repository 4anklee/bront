"""InferenceGuard command-line tools."""

import asyncio
import math
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from dotenv import load_dotenv
from pydantic import HttpUrl
from rich.console import Console
from rich.table import Table

from inference_guard.benchmark import BenchmarkConfig, BenchmarkReport, Scenario, run_benchmark
from inference_guard.compare_cli import compare
from inference_guard.controls import FaultAction, control_fault, inspect_status
from inference_guard.run_cli import run

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.command()(run)
app.command()(compare)


@app.callback()
def main() -> None:
    """Inspect, demonstrate, and measure inference reliability."""


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


# These commands share benchmark URL resolution and deliberately keep persistent
# demo faults separate from the benchmark's automatically restored, unique faults.
def local_url(explicit: str | None, override: str, port_var: str, default: str) -> str:
    return str(
        HttpUrl(
            explicit or os.getenv(override) or f"http://127.0.0.1:{os.getenv(port_var, default)}"
        )
    )


@app.command()
def status(
    gateway_url: Annotated[
        str | None, typer.Option(help="Gateway URL (same defaults as benchmark).")
    ] = None,
    check_backends: Annotated[
        bool,
        typer.Option(
            help="Send one direct inference to each backend; does not change gateway circuit state."
        ),
    ] = False,
    primary_url: Annotated[
        str | None, typer.Option(help="Direct primary URL for active checks.")
    ] = None,
    fallback_url: Annotated[
        str | None, typer.Option(help="Direct fallback URL for active checks.")
    ] = None,
    timeout: Annotated[float, typer.Option(min=0.001, max=300)] = 5,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Observe one gateway instance; optionally check backend inference separately."""
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        if not math.isfinite(timeout):
            raise ValueError("Timeout must be finite")
        if not check_backends and (primary_url or fallback_url):
            raise ValueError("Backend URLs require --check-backends")
        gateway = local_url(gateway_url, "IG_BENCHMARK_GATEWAY_URL", "IG_GATEWAY_PORT", "8000")
        primary = local_url(primary_url, "IG_STATUS_PRIMARY_URL", "IG_PRIMARY_PORT", "8001")
        fallback = local_url(fallback_url, "IG_STATUS_FALLBACK_URL", "IG_FALLBACK_PORT", "8002")

        async def run():
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                return await inspect_status(
                    client,
                    gateway,
                    primary if check_backends else None,
                    fallback if check_backends else None,
                    timeout,
                )

        report = asyncio.run(run())
        if json_output:
            typer.echo(report.model_dump_json(indent=2))
        else:
            console = Console()
            console.print(f"Gateway: {gateway} | instance: {report.instance or 'not reported'}")
            console.print(f"Ready: {report.ready} | observed: {report.observed_at}")
            table = Table(title="Observed circuit state (not backend health)")
            for column in ("Backend", "Circuit", "Consecutive failures", "Direct inference check"):
                table.add_column(column)
            for name, observation in report.backends.items():
                probe = report.active_checks.get(name)
                checked = ("healthy" if probe.healthy else "unhealthy") if probe else "not checked"
                table.add_row(
                    name, observation.state, str(observation.consecutive_failures), checked
                )
            console.print(table)
            for name, probe in report.active_checks.items():
                console.print(f"{name}: {probe.url} ({probe.protocol}), {probe.elapsed_ms:.1f} ms")
                if probe.error:
                    console.print(probe.error, markup=False)
            console.print(
                "One gateway snapshot; a Service URL may select a different pod next time."
            )
            if check_backends:
                console.print(
                    "Direct checks bypass gateway routing and any proxy not in those URLs."
                )
    except (httpx.HTTPError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
        typer.echo(f"Status could not complete: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def fault(
    action: Annotated[
        FaultAction, typer.Argument(help="Persistent fault: outage, delay, or reset.")
    ],
    toxiproxy_url: Annotated[
        str | None, typer.Option(help="Control URL (same defaults as benchmark).")
    ] = None,
    latency_ms: Annotated[int, typer.Option(min=1, max=30000)] = 2000,
) -> None:
    """Control the local Compose primary proxy; reset explicitly after a demo.

    Requires compose.benchmark.yaml. Reset re-enables the primary proxy and removes
    only the inferenceguard-demo-latency toxic. Do not share it with a running benchmark.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        url = local_url(toxiproxy_url, "IG_BENCHMARK_TOXIPROXY_URL", "IG_TOXIPROXY_PORT", "18474")

        async def run():
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                await control_fault(client, url, action, latency_ms)

        asyncio.run(run())
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        typer.echo(f"Fault control could not complete: {exc}", err=True)
        typer.echo(
            "Check compose.benchmark.yaml; run 'inferenceguard fault reset' to repair.", err=True
        )
        raise typer.Exit(2) from exc
    if action == FaultAction.reset:
        typer.echo("Primary proxy enabled; demo delay removed. Circuit recovery needs traffic.")
    else:
        detail = f" ({latency_ms} ms)" if action == FaultAction.delay else ""
        typer.echo(f"Primary proxy {action.value}{detail} injected; persists until 'fault reset'.")


if __name__ == "__main__":
    app()
