"""Baseline experiments against external endpoints, with partial evidence on cancellation."""

import asyncio
from pathlib import Path
from time import monotonic

import httpx

from inference_guard.measurement import drive_load
from inference_guard.results import (
    AssertionResult,
    Attempt,
    Phase,
    RunReport,
    timestamp,
    write_report,
)
from inference_guard.suites import LoadedSuite, SuiteError
from inference_guard.targets import HttpJsonTarget


def make_report(loaded: LoadedSuite) -> RunReport:
    target = loaded.suite.target
    # Do not serialize URL paths/queries, header values, case bodies or expected outputs.
    origin = str(httpx.URL(str(target.url)).copy_with(path="/", query=None))
    return RunReport(
        target={"adapter": target.adapter, "origin": origin, "method": target.method},
        workload=loaded.suite.workload.model_dump(mode="json", exclude={"cases"}),
        cases_sha256=loaded.cases_sha256,
    )


async def run_suite(
    loaded: LoadedSuite,
    output: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunReport:
    if await asyncio.to_thread(output.resolve) in (loaded.source, loaded.cases_path):
        raise SuiteError("Report output must not overwrite the suite or its input cases")
    report = make_report(loaded)
    workload = loaded.suite.workload
    target = HttpJsonTarget(loaded)
    limits = httpx.Limits(
        max_connections=workload.concurrency,
        max_keepalive_connections=workload.concurrency,
        keepalive_expiry=2,
    )
    await asyncio.to_thread(write_report, report, output)  # Check output before any network calls.
    try:
        async with httpx.AsyncClient(
            timeout=workload.request_timeout_seconds,
            limits=limits,
            transport=transport,
            trust_env=False,
        ) as client:

            async def measure(phase: Phase, count: int | None, duration: float | None = None):
                phase_origin = monotonic()

                async def request(index: int, origin: float) -> Attempt:
                    case = loaded.cases[index % len(loaded.cases)]
                    started = monotonic()
                    sample = Attempt(
                        index=index, case=case.name, started_ms=(started - origin) * 1000
                    )
                    phase.samples.append(sample)
                    try:
                        async with asyncio.timeout(workload.request_timeout_seconds):
                            response = await target.send(client, case)
                            sample.status_code = response.status_code
                            result = target.evaluate(response, case)
                            sample.error = result.error
                            sample.reason = result.reason
                            sample.check_index = result.check_index
                            sample.backend = result.backend
                    except (TimeoutError, httpx.TimeoutException) as exc:
                        sample.error = "timeout"
                        sample.error_type = type(exc).__name__
                    except (httpx.TransportError, httpx.DecodingError) as exc:
                        sample.error = "transport"
                        sample.error_type = type(exc).__name__
                    except asyncio.CancelledError:
                        sample.error = "cancelled"
                        raise
                    except Exception as exc:
                        sample.error = "execution"
                        sample.error_type = type(exc).__name__
                        raise
                    finally:
                        sample.duration_ms = (monotonic() - started) * 1000
                    return sample

                try:
                    return await drive_load(
                        request,
                        concurrency=workload.concurrency,
                        count=count,
                        duration_seconds=duration,
                        origin=phase_origin,
                        max_samples=workload.max_samples if phase is report.measurement else None,
                    )
                finally:
                    phase.elapsed_seconds = monotonic() - phase_origin
                    phase.summarize()

            if workload.warmup_requests:
                await measure(report.warmup, workload.warmup_requests)
                if report.warmup.failed:
                    report.status = "failed"
                    report.reason = "warmup_failed"
                    return report
            result = await measure(report.measurement, workload.requests, workload.duration_seconds)
            if result.sample_limit_reached:
                report.status = "error"
                report.reason = "sample_limit_reached"
            elif not report.measurement.requests:
                report.status = "error"
                report.reason = "no_measured_requests"
            else:
                policy = loaded.suite.assertions
                report.assertions.append(
                    AssertionResult(
                        name="max_failed_requests",
                        actual=report.measurement.failed,
                        limit=policy.max_failed_requests,
                        passed=report.measurement.failed <= policy.max_failed_requests,
                    )
                )
                p95 = report.measurement.latency_ms["p95"]
                if policy.max_p95_ms is not None and p95 is not None:
                    report.assertions.append(
                        AssertionResult(
                            name="max_p95_ms",
                            actual=p95,
                            limit=policy.max_p95_ms,
                            passed=p95 <= policy.max_p95_ms,
                        )
                    )
                report.status = "passed" if all(a.passed for a in report.assertions) else "failed"
    except asyncio.CancelledError:
        report.status = "cancelled"
        report.reason = "interrupted"
        raise
    except Exception as exc:
        # Exception strings can contain URLs, credentials, or response bodies.
        report.status = "error"
        report.reason = f"execution_error:{type(exc).__name__}"
    finally:
        report.finished_at = timestamp()
        report.warmup.summarize()
        report.measurement.summarize()
        await asyncio.to_thread(write_report, report, output)
    return report
