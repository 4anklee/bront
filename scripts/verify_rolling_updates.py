"""Restart both gateway replicas under in-cluster traffic, healthy and with Triton offline."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Execute inside the benchmark image using the same measurement/validation code
# as the CLI. Kubernetes controls the outage, so no Toxiproxy or primary warmup
# is appropriate here. Keep logs compact and retain details of failed requests.
PROBE = """
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from inference_guard.benchmark import BenchmarkConfig, BenchmarkRunner, Scenario

async def main():
    duration, concurrency, expected = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    config = BenchmarkConfig(gateway_url="http://gateway:8000", duration_seconds=duration,
                             concurrency=concurrency, warmup=0)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=5, limits=limits, trust_env=False) as client:
        runner = BenchmarkRunner(config, client)
        warmup = await runner.load(Scenario.baseline, 20)
        if warmup.failed or any(s.backend != expected for s in warmup.samples):
            raise RuntimeError("Warmup did not consistently reach " + expected)
        print(json.dumps({"event": "started", "at": datetime.now(UTC).isoformat()}), flush=True)
        result = await runner.measure(Scenario.baseline)
        finished_at = datetime.now(UTC).isoformat()
    result.expectations_met = (not result.failed
                               and all(s.backend == expected for s in result.samples))
    seconds = defaultdict(lambda: {"requests": 0, "failed": 0, "primary": 0, "fallback": 0})
    for sample in result.samples:
        bucket = seconds[int(sample.started_ms / 1000)]
        bucket["requests"] += 1
        bucket["failed"] += sample.error is not None
        if sample.backend is not None:
            bucket[sample.backend] += 1
    print(json.dumps({"event": "finished", "at": finished_at,
                      "expected_backend": expected, "config": config.model_dump(mode="json"),
                      "measurement": result.model_dump(exclude={"samples"}, mode="json"),
                      "per_second": dict(sorted(seconds.items())),
                      "failed_samples": [s.model_dump() for s in result.samples if s.error][:100]}),
          flush=True)
    return 0 if result.expectations_met else 1

sys.exit(asyncio.run(main()))
"""

RECOVERY_PROBE = """
import json
import sys
import time
import urllib.request

expected = sys.argv[1]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    request = urllib.request.Request("http://127.0.0.1:8000/v1/infer",
        data=b'{"inputs":[1,2,3]}', headers={"Content-Type":"application/json"})
    response = json.load(urllib.request.urlopen(request, timeout=5))
    if response == {"model":"double", "outputs":[2,4,6], "backend":expected}:
        assert urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=5).status == 200
        print(json.dumps(response))
        break
    time.sleep(0.25)
else:
    raise RuntimeError("Pod did not recover " + expected + " routing")
"""


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class Verification:
    def __init__(self, context: str, namespace: str, duration: int, concurrency: int):
        self.kubectl = ["kubectl", "--context", context, "--namespace", namespace]
        self.duration = duration
        self.concurrency = concurrency

    def command(self, *args: str, stdin: str | None = None, timeout: int = 180) -> str:
        result = subprocess.run(
            [*self.kubectl, *args],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"kubectl {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result.stdout

    def gateways(self) -> list[dict]:
        pods = json.loads(self.command("get", "pods", "-l", "app=gateway", "-o", "json"))
        return [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "images": [c["imageID"] for c in p["status"]["containerStatuses"]],
            }
            for p in pods["items"]
            if not p["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p["status"].get("conditions", [])
            )
        ]

    def probe_gateways(self, expected: str) -> list[dict]:
        pods = self.gateways()
        if len(pods) != 2:
            raise RuntimeError("Expected two ready gateway pods")
        for pod in pods:
            self.command(
                "exec",
                pod["name"],
                "--",
                "/app/.venv/bin/python",
                "-c",
                RECOVERY_PROBE,
                expected,
                timeout=45,
            )
        return pods

    def events(self, job: str) -> list[dict]:
        output = self.command("logs", f"job/{job}")
        return [json.loads(line) for line in output.splitlines() if line.startswith('{"event":')]

    def run_case(self, expected: str) -> dict:
        before = self.probe_gateways(expected)
        job = json.loads(
            self.command(
                "create",
                "--dry-run=client",
                "-f",
                str(ROOT / "deploy/kubernetes/benchmark-job.yaml"),
                "-o",
                "json",
            )
        )
        job["metadata"] = {"generateName": f"rollout-{expected}-"}
        job["spec"]["activeDeadlineSeconds"] = self.duration + 120
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["command"] = ["/app/.venv/bin/python", "-u", "-c"]
        container["args"] = [PROBE, str(self.duration), str(self.concurrency), expected]
        name = json.loads(self.command("create", "-f", "-", "-o", "json", stdin=json.dumps(job)))[
            "metadata"
        ]["name"]
        print(f"{expected}: waiting for traffic in {name}", flush=True)
        try:
            self.command(
                "wait",
                "--for=condition=Ready",
                "pod",
                "-l",
                f"job-name={name}",
                "--timeout=60s",
                timeout=70,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                events = self.events(name)
                if any(e["event"] == "started" for e in events):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "Benchmark did not start: " + self.command("logs", f"job/{name}")
                )
            time.sleep(5)  # Measured traffic before the restart.
            restarted_at = timestamp()
            print(f"{expected}: restarting gateway during measured traffic", flush=True)
            self.command("rollout", "restart", "deployment/gateway")
            self.command("rollout", "status", "deployment/gateway", "--timeout=90s", timeout=100)
            rollout_finished_at = timestamp()
            after = self.probe_gateways(expected)
            replaced = len(after) == 2 and not (
                {p["uid"] for p in before} & {p["uid"] for p in after}
            )
            deadline = time.monotonic() + self.duration + 30
            while time.monotonic() < deadline:
                events = self.events(name)
                finished = next((e for e in events if e["event"] == "finished"), None)
                if finished is not None:
                    break
                status = json.loads(self.command("get", "job", name, "-o", "json"))["status"]
                if status.get("failed"):
                    raise RuntimeError(self.command("logs", f"job/{name}"))
                time.sleep(2)
            else:
                raise RuntimeError("Benchmark did not finish")
            started = next(e for e in events if e["event"] == "started")
            # Reject a test where load ended before the rollout completed.
            overlapping = started["at"] < restarted_at < rollout_finished_at < finished["at"]
            measurement = finished["measurement"]
            passed = measurement["expectations_met"] and replaced and overlapping
            print(
                f"{expected}: {measurement['successful']}/{measurement['requests']} successful; "
                f"errors={measurement['errors']}; passed={passed}",
                flush=True,
            )
            return {
                "job": name,
                "pods_before": before,
                "pods_after": after,
                "restarted_at": restarted_at,
                "rollout_finished_at": rollout_finished_at,
                "load_started_at": started["at"],
                "rollout_overlapped_load": overlapping,
                "pods_replaced": replaced,
                "passed": passed,
                **finished,
            }
        finally:
            # Only remove the Job created by this invocation; results are saved locally.
            self.command("delete", "job", name, "--wait=false")


def main(context: str, namespace: str, duration: int, concurrency: int, output: Path) -> bool:
    check = Verification(context, namespace, duration, concurrency)
    deployment = json.loads(check.command("get", "deployment/triton", "-o", "json"))
    replicas = deployment["spec"]["replicas"]
    gateway = json.loads(check.command("get", "deployment/gateway", "-o", "json"))
    if replicas != 1 or gateway["spec"]["replicas"] != 2:
        raise RuntimeError("Expected one Triton and two gateway replicas")
    for name in ("gateway", "triton", "fallback"):
        check.command("rollout", "status", f"deployment/{name}", "--timeout=120s")
    report: dict = {
        "started_at": timestamp(),
        "context": context,
        "namespace": namespace,
        "gateway_spec": gateway["spec"],
        "runs": [],
        "passed": False,
    }
    try:
        report["runs"].append(check.run_case("primary"))
        try:
            print("Scaling Triton to zero for the fallback rolling-update check", flush=True)
            check.command("scale", "deployment/triton", "--replicas=0")
            check.command("wait", "--for=delete", "pod", "-l", "app=triton", "--timeout=120s")
            report["runs"].append(check.run_case("fallback"))
        finally:
            print("Restoring Triton and verifying primary recovery on both gateways", flush=True)
            recovery_started = time.monotonic()
            check.command("scale", "deployment/triton", f"--replicas={replicas}")
            check.command("rollout", "status", "deployment/triton", "--timeout=120s")
            report["recovered_pods"] = check.probe_gateways("primary")
            report["recovery_seconds"] = time.monotonic() - recovery_started
        report["passed"] = all(run["passed"] for run in report["runs"])
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report saved to {output}", flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="kind-inference-guard")
    parser.add_argument("--namespace", default="inference-guard")
    parser.add_argument(
        "--duration", type=int, default=60, choices=range(45, 301), metavar="45..300"
    )
    parser.add_argument(
        "--concurrency", type=int, default=20, choices=range(1, 101), metavar="1..100"
    )
    parser.add_argument("--output", type=Path, default=Path("reports/rolling-updates.json"))
    args = parser.parse_args()
    raise SystemExit(
        0 if main(args.context, args.namespace, args.duration, args.concurrency, args.output) else 1
    )
