"""Bront: repeatable inference experiments, release comparisons, and reports."""

import typer

from bront.compare_cli import compare
from bront.report_cli import report
from bront.run_cli import run

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.command()(run)
app.command()(compare)
app.command()(report)


@app.callback()
def main() -> None:
    """Validate inference deployments with repeatable experiments and release evidence."""


if __name__ == "__main__":
    app()
