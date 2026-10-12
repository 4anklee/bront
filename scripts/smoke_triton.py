"""Verify Triton and fallback parity, then stop/restart the Compose primary."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    [1.0, 2.0, 3.0],
    [0.1, -2.25, 0.0, 1e100, 1e-100],
    [3.5],
    [float(i) for i in range(1024)],
    [5e-324, 1e-310, -0.0, sys.float_info.max / 2],
]


def main(gateway: str, primary: str, project: str | None) -> None:
    compose = ["docker", "compose", "-f", str(ROOT / "compose.triton.yaml")]
    if project:
        compose.extend(["--project-name", project])

    def control(action: str) -> None:
        subprocess.run([*compose, action, "primary"], cwd=ROOT, check=True, timeout=120)

    with httpx.Client(timeout=5, trust_env=False) as client:

        def ready() -> None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    response = client.get(f"{primary}/v2/models/double/versions/1/ready")
                    if response.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.5)
            raise RuntimeError("Triton model did not become ready within 120 seconds")

        def infer(values: list[float], backend: str) -> None:
            response = client.post(
                f"{gateway}/v1/infer", json={"model": "double", "inputs": values}
            )
            response.raise_for_status()
            expected = {"model": "double", "outputs": [x * 2 for x in values], "backend": backend}
            if response.json() != expected:
                raise RuntimeError(f"Unexpected inference response: {response.text}")

        ready()

        # Allow a previous run's open circuit to probe before asserting primary routing.
        def recovered() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                response = client.post(f"{gateway}/v1/infer", json={"inputs": CASES[0]})
                response.raise_for_status()
                if response.json() == {
                    "model": "double",
                    "outputs": [2, 4, 6],
                    "backend": "primary",
                }:
                    return
                time.sleep(0.5)
            raise RuntimeError("Gateway did not recover primary routing within 30 seconds")

        recovered()
        for values in CASES:
            infer(values, "primary")
        print(
            "PASS: Triton FP64 inference (fractions, negatives, large values, 1–1024 inputs)",
            flush=True,
        )

        try:
            control("stop")
            for values in CASES:
                infer(values, "fallback")
            response = client.get(f"{gateway}/status")
            response.raise_for_status()
            if response.json()["backends"]["primary"]["state"] != "open":
                raise RuntimeError("Primary circuit did not open during the Triton outage")
            print("PASS: identical results from fallback while Triton is stopped", flush=True)
        finally:
            control("start")
            ready()

        recovered()
        for values in CASES:
            infer(values, "primary")
        print("PASS: Triton recovery with unchanged outputs", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    parser.add_argument("--primary-url", default="http://127.0.0.1:8001")
    parser.add_argument("--project-name", help="Match a custom docker compose --project-name")
    args = parser.parse_args()
    main(args.gateway_url, args.primary_url, args.project_name)
