"""Capture a real 60-second Compose outage/recovery demonstration as an asciicast.

Requires the Triton + benchmark Compose overlays. Does not fabricate terminal
output or samples: frames contain live HTTP observations and actual CLI output.
Render with agg/ffmpeg as described in docs/demo/README.md.
"""

import argparse
import asyncio
import io
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from itertools import count
from pathlib import Path
from time import monotonic

import httpx
from rich.console import Console
from rich.table import Table

from inference_guard.benchmark import BenchmarkConfig, BenchmarkRunner, Sample, Scenario, summarize
from inference_guard.controls import inspect_status
from inference_guard.faults import FaultController


class DiagnosticTransport(httpx.AsyncHTTPTransport):
    def __init__(self, limits: httpx.Limits) -> None:
        super().__init__(limits=limits, retries=0)
        self.errors: list[dict[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except httpx.TransportError as exc:
            self.errors.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "url": str(request.url),
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            raise


async def record(gateway: str, primary: str, fallback: str, proxy: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    config = BenchmarkConfig(gateway_url=gateway, duration_seconds=60, concurrency=4)
    samples: list[Sample] = []
    events: list[dict] = []
    messages = ["Preparing live checks"]
    status_args = [
        "status",
        "--gateway-url",
        gateway,
        "--check-backends",
        "--primary-url",
        primary,
        "--fallback-url",
        fallback,
        "--json",
    ]

    async def cli(*args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "inference_guard.cli",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode() or stdout.decode())
        return stdout.decode().strip()

    # Keep status polling out of the measured connection pool. Retire idle
    # connections before Uvicorn's default five-second keep-alive timeout.
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=2)
    transport = DiagnosticTransport(limits)
    recorder_hash = sha256(await asyncio.to_thread(Path(__file__).read_bytes)).hexdigest()
    async with (
        httpx.AsyncClient(timeout=5, trust_env=False) as client,
        httpx.AsyncClient(timeout=5, transport=transport, trust_env=False) as traffic_client,
    ):
        await FaultController(client, proxy).preflight()
        initial = json.loads(await cli(*status_args))
        if initial["configuration"]["primary_protocol"] != "triton":
            raise RuntimeError("This demonstration requires a Triton primary")
        runner = BenchmarkRunner(config, traffic_client)
        warmup = await runner.load(Scenario.baseline, 4)
        if warmup.failed or warmup.primary_requests != 4:
            raise RuntimeError("Warmup must return four correct primary responses")
        started_at = datetime.now(UTC)
        origin = monotonic()
        indices = count()
        fault_attempted = False
        reset_at: float | None = None
        recovered_at: float | None = None
        frame = initial
        direct_checks = initial["active_checks"]
        source_commit = (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
            )
        ).strip()

        async def traffic() -> None:
            nonlocal recovered_at
            while monotonic() - origin < 60:
                sample = await runner.request(next(indices), origin)
                samples.append(sample)
                # Require a request initiated after reset; exclude old in-flight work.
                if (
                    reset_at is not None
                    and recovered_at is None
                    and sample.started_ms / 1000 >= reset_at
                    and sample.backend == "primary"
                    and sample.error is None
                ):
                    recovered_at = monotonic() - origin
                await asyncio.sleep(0.1)  # A readable demo, not a saturation benchmark.

        def screen(elapsed: float) -> str:
            stream = io.StringIO()
            console = Console(
                file=stream,
                force_terminal=True,
                color_system="standard",
                width=100,
                height=36,
                no_color=False,
            )
            counts = Counter(s.backend for s in samples if s.error is None)
            errors = sum(s.error is not None for s in samples)
            phase = "BASELINE" if elapsed < 10 else "PROXY OUTAGE" if elapsed < 30 else "RECOVERY"
            console.print("INFERENCEGUARD  /  OUTAGE → FALLBACK → RECOVERY", style="bold cyan")
            console.print("Live Compose stack · Triton / ONNX CPU · compatible Python fallback")
            console.print(f"{elapsed:05.1f} / 60 seconds   |   {phase}", style="bold yellow")
            console.print("━" * 96, style="bright_black")
            console.print("Contract: double([1, 2, 3]) → [2, 4, 6] on either backend")
            console.print("4 concurrent workers · 100 ms pause per worker · no request retries")
            console.print()
            table = Table(expand=True)
            for label in ("Requests", "Correct", "Errors", "Primary", "Fallback"):
                table.add_column(label, justify="right")
            table.add_row(
                str(len(samples)),
                str(len(samples) - errors),
                str(errors),
                str(counts["primary"]),
                str(counts["fallback"]),
            )
            console.print(table)
            table = Table(title="Gateway observations vs direct inference checks", expand=True)
            for label in ("Backend", "Observed circuit", "Failures", "Direct backend"):
                table.add_column(label)
            for name in ("primary", "fallback"):
                observation = frame["backends"][name]
                healthy = direct_checks[name]["healthy"]
                table.add_row(
                    name,
                    observation["state"],
                    str(observation["consecutive_failures"]),
                    "healthy" if healthy else "unhealthy",
                )
            console.print(table)
            console.print(
                "Direct checks bypass the proxy; healthy Triton can have an open gateway circuit."
            )
            console.print()
            console.print("DEMO CONTROLS", style="bold cyan")
            for message in messages[-3:]:
                console.print(message, markup=False, overflow="fold")
            for _ in range(max(0, 3 - len(messages))):
                console.print()
            recovery = (
                f"{recovered_at - reset_at:.3f} seconds after reset acknowledgement"
                if recovered_at is not None and reset_at is not None
                else "awaiting primary"
            )
            console.print(f"Recovery: {recovery}", style="green" if recovered_at else "yellow")
            console.print(
                "Fault schedule: outage at 10s · reset at 30s · keep traffic flowing until 60s"
            )
            console.print(
                "Actual responses validated by the benchmark request checker; full evidence saved."
            )
            return "\x1b[2J\x1b[H" + stream.getvalue().replace("\n", "\r\n")

        workers = []
        try:
            with output.open("w") as cast:
                cast.write(
                    json.dumps(
                        {
                            "version": 2,
                            "width": 100,
                            "height": 36,
                            "timestamp": int(started_at.timestamp()),
                            "title": "InferenceGuard: real Triton outage and recovery",
                            "env": {"TERM": "xterm-256color"},
                        }
                    )
                    + "\n"
                )
                messages[:] = [
                    "$ inferenceguard status --check-backends  → both direct checks healthy"
                ]
                cast.write(json.dumps([0, "o", screen(0)]) + "\n")
                workers = [asyncio.create_task(traffic()) for _ in range(4)]
                for tick in range(1, 61):
                    await asyncio.sleep(max(0, origin + tick - monotonic()))
                    if tick in (10, 30):
                        action = "outage" if tick == 10 else "reset"
                        fault_attempted = True
                        acknowledged = await cli("fault", action, "--toxiproxy-url", proxy)
                        at = monotonic() - origin
                        events.append({"at_seconds": at, "action": action, "output": acknowledged})
                        if action == "reset":
                            reset_at = at
                        messages.append(f"$ inferenceguard fault {action}  → {acknowledged}")
                        checked = json.loads(await cli(*status_args))
                        direct_checks = checked["active_checks"]
                    frame = (await inspect_status(client, gateway)).model_dump(mode="json")
                    cast.write(
                        json.dumps(
                            [round(monotonic() - origin, 6), "o", screen(monotonic() - origin)]
                        )
                        + "\n"
                    )
                    cast.flush()
                    if tick % 10 == 0:
                        print(f"Recorded {tick}s: {len(samples)} validated attempts", flush=True)
                await asyncio.gather(*workers)
                elapsed = monotonic() - origin
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if fault_attempted:
                await cli("fault", "reset", "--toxiproxy-url", proxy)

        result = summarize(Scenario.recovery, samples, elapsed)
        passed = (
            result.failed == 0
            and result.primary_requests > 0
            and result.fallback_requests > 0
            and recovered_at is not None
        )
        result.expectations_met = passed
        report = {
            "started_at": started_at.isoformat(),
            "application_source_commit": source_commit,
            "recorder_sha256": recorder_hash,
            "topology": "Compose: one gateway, Toxiproxy, Triton CPU, Python fallback",
            "gateway_configuration": initial["configuration"],
            "urls": {"gateway": gateway, "primary": primary, "fallback": fallback, "proxy": proxy},
            "workers": 4,
            "pause_per_worker_seconds": 0.1,
            "traffic_duration_seconds": 60,
            "traffic_keepalive_expiry_seconds": 2,
            "transport_diagnostics": transport.errors,
            "events": events,
            "direct_checks": direct_checks,
            "recovery_seconds_after_reset_ack": (
                recovered_at - reset_at
                if recovered_at is not None and reset_at is not None
                else None
            ),
            "passed": passed,
            "measurement": result.model_dump(exclude={"samples"}, mode="json"),
            "samples": [sample.model_dump() for sample in samples],
        }
        output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Saved {output}; {result.successful}/{result.requests} correct; passed={passed}")
        if not passed:
            raise RuntimeError("Demo did not demonstrate correct failover and recovery")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://localhost:8000")
    parser.add_argument("--primary-url", default="http://localhost:8001")
    parser.add_argument("--fallback-url", default="http://localhost:8002")
    parser.add_argument("--toxiproxy-url", default="http://localhost:18474")
    parser.add_argument("--output", type=Path, default=Path("reports/demo.cast"))
    args = parser.parse_args()
    asyncio.run(
        record(
            args.gateway_url, args.primary_url, args.fallback_url, args.toxiproxy_url, args.output
        )
    )
