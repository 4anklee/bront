"""Offline HTML artifact rendering; rendering does not rerun release decisions."""

from pathlib import Path
from typing import Annotated

import typer

from bront.artifacts import write_text_artifact
from bront.html_reports import render_file
from bront.suites import SuiteError


def report(
    source: Annotated[Path, typer.Argument(help="Run, series manifest, or comparison JSON.")],
    output: Annotated[Path, typer.Option(help="Self-contained HTML file.")] = Path(
        "reports/review.html"
    ),
) -> None:
    """Render existing evidence as shareable HTML; no service connections or traffic."""
    try:
        html = render_file(source, output)
        write_text_artifact(html, output)
    except SuiteError as exc:
        typer.echo(f"Cannot render report: {exc}", err=True)
        raise typer.Exit(2) from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        typer.echo(
            f"Cannot render report ({type(exc).__name__}); check the artifact schema and output.",
            err=True,
        )
        raise typer.Exit(2) from None
    typer.echo(f"HTML report: {output}")
