"""Exercise the installed CLI against an independent HTTP server, including SIGINT."""

import json
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "external"


def command(suite: Path, url: str, name: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "bront.cli",
        "run",
        str(suite),
        "--url",
        url,
        "--output",
        str(REPORTS / f"{name}.json"),
        "--json",
    ]


def verify_comparisons(url: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        suite = temporary / "suite.toml"
        example = ROOT / "examples/http-json"
        (temporary / "cases.jsonl").write_text((example / "cases.jsonl").read_text())
        suite.write_text(
            (example / "suite.toml").read_text()
            + f"""
[metadata]
model_revision = "sum-fixture-v1"
serving_revision = "stdlib-fixture-v1"
architecture = "{platform.machine()}"
[metadata.resources]
cpu = "shared-smoke-host"
memory = "shared-smoke-host"
[metadata.labels]
environment = "smoke"
"""
        )
        limits = temporary / "limits.toml"
        limits.write_text('version = 1\nminimum_requests = 30\nrequired_labels = ["environment"]\n')
        for name in ("comparison-baseline", "comparison-candidate"):
            subprocess.run(
                [*command(suite, url, name), "--repeat", "3"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        left = REPORTS / "comparison-baseline.json"
        right = REPORTS / "comparison-candidate.json"

        def compare(first: Path, second: Path, name: str, code: int, status: str) -> None:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bront.cli",
                    "compare",
                    str(first),
                    str(second),
                    "--policy",
                    str(limits),
                    "--output",
                    str(REPORTS / f"{name}.json"),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == code, result.stderr
            assert json.loads(result.stdout)["status"] == status

        compare(left, right, "comparison-pass", 0, "passed")
        first_left = REPORTS / json.loads(left.read_text())["runs"][0]["path"]
        first_right = REPORTS / json.loads(right.read_text())["runs"][0]["path"]
        compare(first_left, first_right, "comparison-single-run", 2, "inconclusive")
        suite.write_text(suite.read_text().replace("concurrency = 4", "concurrency = 1"))
        subprocess.run(
            [*command(suite, url, "comparison-changed-workload"), "--repeat", "3"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        compare(
            left,
            REPORTS / "comparison-changed-workload.json",
            "comparison-incompatible",
            2,
            "incompatible",
        )


def verify_html_reports() -> None:
    for name in (
        "baseline",
        "incorrect-output",
        "interrupted",
        "comparison-baseline",
        "comparison-pass",
        "comparison-single-run",
        "comparison-incompatible",
    ):
        output = REPORTS / f"{name}.html"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "bront.cli",
                "report",
                str(REPORTS / f"{name}.json"),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        html = output.read_text()
        assert html.startswith("<!doctype html>")
        assert "data:application/json;base64," in html
        assert "<script" not in html
    print("Offline HTML reports passed: runs, partial series, and comparison verdicts.")


def main() -> None:
    with subprocess.Popen(
        [sys.executable, str(ROOT / "examples/http-json/server.py"), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            assert server.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(server.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise RuntimeError("HTTP fixture did not start")
                url = server.stdout.readline().strip()
            if not url.startswith("http://127.0.0.1:"):
                raise RuntimeError("HTTP fixture failed to publish its URL")
            suite = ROOT / "examples/http-json/suite.toml"
            passed = subprocess.run(
                command(suite, url, "baseline"),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            report = json.loads(passed.stdout)
            assert report["status"] == "passed"
            assert report["measurement"]["successful"] == 30
            assert report["warmup"]["requests"] == 2

            with tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                altered_suite = temporary / "suite.toml"
                cases = (suite.parent / "cases.jsonl").read_text()
                # A wrong answer must fail despite HTTP 200.
                (temporary / "cases.jsonl").write_text(cases.replace('"equals":6', '"equals":999'))
                altered_suite.write_text(
                    suite.read_text().replace("warmup_requests = 2", "warmup_requests = 0")
                )
                failed = subprocess.run(
                    command(altered_suite, url, "incorrect-output"),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert failed.returncode == 1, failed.stderr
                assert json.loads(failed.stdout)["measurement"]["errors"] == {"validation": 10}

                (temporary / "cases.jsonl").write_text(cases)
                altered_suite.write_text(
                    suite.read_text().replace("requests = 30", "duration_seconds = 60")
                )
                partial_path = REPORTS / "interrupted.json"
                partial_path.unlink(missing_ok=True)
                with subprocess.Popen(
                    [*command(altered_suite, url, "interrupted"), "--repeat", "3"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as running:
                    try:
                        deadline = time.monotonic() + 10
                        while not partial_path.exists():
                            if running.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError("Duration run failed to start")
                            time.sleep(0.02)
                        time.sleep(0.2)
                        running.send_signal(signal.SIGINT)
                        _, stderr = running.communicate(timeout=10)
                        assert running.returncode == 130, stderr
                        partial = json.loads(partial_path.read_text())
                        assert partial["status"] == "cancelled"
                        assert partial["finished_at"] is not None
                        assert partial["runs"]
                        child = json.loads((REPORTS / partial["runs"][0]["path"]).read_text())
                        assert child["status"] == "cancelled"
                        assert child["measurement"]["requests"] > 0
                    finally:
                        if running.poll() is None:
                            running.kill()
                        running.wait(timeout=10)
            verify_comparisons(url)
            verify_html_reports()
            print(
                "Repeated comparisons passed: compatible, insufficient, and incompatible evidence."
            )
            print("External HTTP smoke passed: baseline, incorrect output, and interrupted run.")
            print(f"Reports: {REPORTS}")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
