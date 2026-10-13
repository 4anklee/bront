"""Sequential repeated runs with an incremental manifest and retained child reports."""

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from inference_guard.artifacts import write_artifact
from inference_guard.provenance import digest
from inference_guard.results import RunReport, timestamp
from inference_guard.runs import run_suite
from inference_guard.suites import LoadedSuite, SuiteError
from inference_guard.validation import StrictModel


class RunReference(StrictModel):
    path: str
    run_id: str
    sha256: str


class SeriesReport(StrictModel):
    report_type: Literal["bront.series"] = "bront.series"
    schema_version: Literal[1] = 1
    series_id: str = Field(default_factory=lambda: str(uuid4()))
    started_at: str = Field(default_factory=timestamp)
    finished_at: str | None = None
    requested_runs: int = Field(ge=2, le=100)
    status: Literal["running", "passed", "failed", "error", "cancelled"] = "running"
    reason: str | None = None
    runs: list[RunReference] = Field(default_factory=list)


async def run_series(loaded: LoadedSuite, output: Path, repeat: int) -> SeriesReport:
    if await asyncio.to_thread(output.resolve) in (loaded.source, loaded.cases_path):
        raise SuiteError("Series output must not overwrite the suite or its input cases")
    series = SeriesReport(requested_runs=repeat)
    directory = output.parent / f"{output.stem}-runs-{series.series_id}"
    await asyncio.to_thread(write_artifact, series, output)
    try:
        for index in range(repeat):
            child = directory / f"{index + 1:03}.json"
            report: RunReport | None = None
            try:
                report = await run_suite(loaded, child)
            finally:
                if report is None and await asyncio.to_thread(child.exists):
                    report = RunReport.model_validate_json(await asyncio.to_thread(child.read_text))
                if report is not None:
                    series.runs.append(
                        RunReference(
                            path=str(child.relative_to(output.parent)),
                            run_id=report.provenance.run_id,
                            sha256=digest(report.model_dump(mode="json")),
                        )
                    )
                    await asyncio.to_thread(write_artifact, series, output)
            if report is None or report.status in ("error", "cancelled", "running"):
                series.status = "error"
                series.reason = "run_did_not_complete"
                break
            if report.status == "failed":
                series.status = "failed"
        else:
            if series.status == "running":
                series.status = "passed"
    except asyncio.CancelledError:
        series.status = "cancelled"
        series.reason = "interrupted"
        raise
    except Exception:
        series.status = "error"
        series.reason = "series_execution_error"
    finally:
        series.finished_at = timestamp()
        await asyncio.to_thread(write_artifact, series, output)
    return series
