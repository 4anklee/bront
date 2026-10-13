"""Exercise the installed CLI against an independent HTTP server, including SIGINT."""

import json
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
        "inference_guard.cli",
        "run",
        str(suite),
        "--url",
        url,
        "--output",
        str(REPORTS / f"{name}.json"),
        "--json",
    ]


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
                    command(altered_suite, url, "interrupted"),
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
                        assert partial["measurement"]["requests"] > 0
                    finally:
                        if running.poll() is None:
                            running.kill()
                        running.wait(timeout=10)
            print("External HTTP smoke passed: baseline, incorrect output, and interrupted run.")
            print(f"Reports: {REPORTS}")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
