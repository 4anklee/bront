"""External endpoint experiments, with bounded scenarios and partial evidence."""

import asyncio
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from pydantic import JsonValue

from inference_guard.artifacts import write_artifact
from inference_guard.checks import evaluate_measurement
from inference_guard.measurement import drive_load
from inference_guard.provenance import RunProvenance, digest, runner_identity
from inference_guard.results import (
    Attempt,
    Phase,
    RunReport,
    timestamp,
)
from inference_guard.scenarios import ScenarioSession, failure_code
from inference_guard.suites import LoadedSuite, SuiteError
from inference_guard.targets import HttpJsonTarget


def make_report(loaded: LoadedSuite) -> RunReport:
    target = loaded.suite.target
    # Do not serialize URL paths/queries, header values, case bodies or expected outputs.
    origin = str(httpx.URL(str(target.url)).copy_with(path="/", query=None))
    suite = loaded.suite
    credential_names = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}
    headers: dict[str, JsonValue] = {
        name.lower(): {"credential": True}
        if not isinstance(value, str) or name.lower() in credential_names
        else {"value_sha256": digest(value)}
        for name, value in target.headers.items()
    }
    url = httpx.URL(str(target.url))
    contract: dict[str, JsonValue] = {
        "adapter": target.adapter,
        "method": target.method,
        "route_sha256": digest({"path": url.path, "query": url.query.decode("ascii")}),
        "headers": headers,
        "backend": target.backend.model_dump(mode="json") if target.backend else None,
        "cases_sha256": loaded.cases_sha256,
        "case_names": [case.name for case in loaded.cases],
        "workload": suite.workload.model_dump(mode="json", exclude={"cases"}),
        "assertions": suite.assertions.model_dump(mode="json"),
        "scenario": suite.scenario.model_dump(mode="json", exclude={"url"})
        if suite.scenario
        else None,
        "measurement": {
            "load": "closed-loop",
            "latency": "all-attempts",
            "percentile": "nearest-rank",
        },
    }
    return RunReport(
        target={"adapter": target.adapter, "origin": origin, "method": target.method},
        workload=loaded.suite.workload.model_dump(mode="json", exclude={"cases"}),
        cases_sha256=loaded.cases_sha256,
        provenance=RunProvenance(
            run_id=str(uuid4()),
            runner=runner_identity(),
            contract=contract,
            contract_sha256=digest(contract),
            deployment=suite.metadata.model_copy(deep=True),
            missing_metadata=suite.metadata.missing(),
        ),
    )


async def run_suite(
    loaded: LoadedSuite,
    output: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    control_transport: httpx.AsyncBaseTransport | None = None,
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
    await asyncio.to_thread(
        write_artifact, report, output
    )  # Check output before any network calls.
    session = None
    try:
        if loaded.suite.scenario is not None:
            session = ScenarioSession(
                loaded.suite.scenario,
                transport=control_transport,
                require_identity=loaded.suite.assertions.preferred_backend is not None,
            )
            report.scenario = session.evidence
            await session.preflight()
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

                async def load():
                    return await drive_load(
                        request,
                        concurrency=workload.concurrency,
                        count=count,
                        duration_seconds=duration,
                        origin=phase_origin,
                        max_samples=workload.max_samples if phase is report.measurement else None,
                    )

                try:
                    if session is not None and phase is report.measurement:
                        return await session.measure(load, phase_origin, phase)
                    return await load()
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
                report.assertions = evaluate_measurement(report.measurement, policy)
                if session is not None:
                    report.assertions.extend(
                        session.recovery(
                            report.measurement, policy, loaded.suite.target.backend is not None
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
        report.reason = f"execution_error:{failure_code(exc)}"
    finally:

        async def finalize() -> None:
            if session is not None:
                try:
                    if not await session.cleanup():
                        report.status = "error"
                        report.reason = "scenario_cleanup_failed"
                finally:
                    try:
                        await session.close()
                    except Exception:
                        report.status = "error"
                        report.reason = "scenario_control_close_failed"
            report.finished_at = timestamp()
            report.warmup.summarize()
            report.measurement.summarize()
            await asyncio.to_thread(write_artifact, report, output)

        # A first interrupt during finalization must not abandon an owned fault.
        finalization = asyncio.create_task(finalize())
        try:
            await asyncio.shield(finalization)
        except asyncio.CancelledError:
            if report.status != "error":
                report.status = "cancelled"
                report.reason = "interrupted"
            await finalization
            await asyncio.to_thread(write_artifact, report, output)
            raise
    return report
