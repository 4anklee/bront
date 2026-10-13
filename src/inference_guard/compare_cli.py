"""CLI for reproducible baseline/candidate release decisions."""

import tomllib
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from inference_guard.comparison_models import ComparisonPolicy
from inference_guard.comparisons import compare_files
from inference_guard.suites import SuiteError


def compare(
    baseline: Annotated[Path, typer.Argument(help="Baseline run or repeated-run manifest.")],
    candidate: Annotated[Path, typer.Argument(help="Candidate run or repeated-run manifest.")],
    policy: Annotated[Path, typer.Option(help="TOML evidence and regression policy.")],
    baseline_run: Annotated[
        list[Path] | None, typer.Option(help="Additional baseline run; repeatable.")
    ] = None,
    candidate_run: Annotated[
        list[Path] | None, typer.Option(help="Additional candidate run; repeatable.")
    ] = None,
    output: Annotated[Path, typer.Option(help="Comparison JSON artifact.")] = Path(
        "reports/comparison.json"
    ),
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the comparison as JSON.")
    ] = False,
) -> None:
    """Compare compatible repeated runs; exit 2 when evidence cannot support a verdict."""
    try:
        if output.resolve() == policy.resolve():
            raise SuiteError("Comparison output must not overwrite its policy")
        if policy.stat().st_size > 1_048_576:
            raise SuiteError("Policy exceeds the 1 MiB limit")
        settings = ComparisonPolicy.model_validate(tomllib.loads(policy.read_text()))
        report = compare_files(
            [baseline, *(baseline_run or [])], [candidate, *(candidate_run or [])], settings, output
        )
    except SuiteError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from None
    except (ValueError, OSError) as exc:
        typer.echo(
            f"Cannot compare artifacts ({type(exc).__name__}); check policy and output.", err=True
        )
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console = Console(markup=False)
        console.print(
            f"{report.status.upper()}: {len(report.baseline_run_ids)} baseline runs, "
            f"{len(report.candidate_run_ids)} candidate runs"
        )
        for difference in report.differences[:10]:
            console.print(
                f"{'Allowed' if difference.allowed else 'Incompatible'} change: {difference.field}"
            )
        for issue in report.issues[:10]:
            console.print(f"Evidence: {issue}")
        for name, metric in report.metrics.items():
            console.print(
                f"{name}: baseline median {metric.baseline.median:.3g} "
                f"[{metric.baseline.minimum:.3g}, {metric.baseline.maximum:.3g}]; "
                f"candidate median {metric.candidate.median:.3g} "
                f"[{metric.candidate.minimum:.3g}, {metric.candidate.maximum:.3g}] {metric.unit}"
            )
        for check in report.checks:
            console.print(
                f"{check.name}: {check.status}; limit {check.limit:g}"
                + (f" ({check.reason})" if check.reason else "")
            )
        if any(run["status"] == "failed" for run in report.run_summaries):
            console.print(
                "One or more runs failed their own release assertions; see run_summaries."
            )
        console.print(f"Report: {output}")
    raise typer.Exit({"passed": 0, "failed": 1}.get(report.status, 2))
