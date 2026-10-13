"""CLI for external HTTP endpoint experiments."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from inference_guard.runs import run_suite
from inference_guard.series import SeriesReport, run_series
from inference_guard.suites import SuiteError, load_suite


def run(
    suite: Annotated[
        Path, typer.Argument(help="TOML suite with target, workload, and assertions.")
    ],
    output: Annotated[Path, typer.Option(help="JSON report; also saved on interruption.")] = Path(
        "reports/run.json"
    ),
    url: Annotated[str | None, typer.Option(help="Override the complete target URL.")] = None,
    repeat: Annotated[
        int, typer.Option(min=1, max=100, help="Sequential fresh runs; >1 writes a manifest.")
    ] = 1,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Validate and measure an external HTTP JSON endpoint."""
    try:
        loaded = load_suite(suite, url=url)
        report = (
            asyncio.run(run_suite(loaded, output))
            if repeat == 1
            else asyncio.run(run_series(loaded, output, repeat))
        )
    except SuiteError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from None
    except OSError as exc:
        typer.echo(f"Cannot write report ({type(exc).__name__}).", err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        typer.echo(f"Interrupted. Check {output} for partial results.", err=True)
        raise typer.Exit(130) from None
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    elif isinstance(report, SeriesReport):
        typer.echo(
            f"{report.status.upper()}: {len(report.runs)}/{report.requested_runs} runs; "
            f"manifest: {output}"
        )
    else:
        console = Console(markup=False)
        measured = report.measurement
        p95 = measured.latency_ms["p95"]
        latency = f"{p95:.2f} ms" if p95 is not None else "unobserved"
        console.print(
            f"{report.status.upper()}: {measured.successful}/{measured.requests} checks passed; "
            f"{measured.requests_per_second:.1f} req/s; p95 {latency}"
        )
        console.print(f"Errors: {measured.errors}; warmup: {report.warmup.requests} requests")
        if report.scenario is not None:
            console.print(f"Scenario: {report.scenario.kind}; cleanup: {report.scenario.cleanup}")
        if report.reason:
            console.print(f"Reason: {report.reason}")
        console.print(f"Report: {output}")
    raise typer.Exit({"passed": 0, "failed": 1}.get(report.status, 2))
