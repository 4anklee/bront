"""Verify a running, disposable demo stack: uv run python scripts/smoke_compose.py."""

import argparse
import time

import httpx

GATEWAY = "http://127.0.0.1:8000"
PRIMARY = "http://127.0.0.1:8001"
FALLBACK = "http://127.0.0.1:8002"
PAYLOAD = {"model": "double", "inputs": [1, 2, 3]}


def main(gateway: str = GATEWAY, primary: str = PRIMARY, fallback: str = FALLBACK) -> None:
    with httpx.Client(timeout=5, trust_env=False) as client:

        def faults(url: str, **settings: bool | float) -> None:
            client.put(f"{url}/faults", json=settings).raise_for_status()

        def infer(expected_backend: str) -> None:
            response = client.post(f"{gateway}/v1/infer", json=PAYLOAD)
            response.raise_for_status()
            expected = {"model": "double", "outputs": [2, 4, 6], "backend": expected_backend}
            if response.json() != expected:
                raise RuntimeError(f"Unexpected inference response: {response.text}")

        for url in (gateway, primary, fallback):
            client.get(f"{url}/healthz").raise_for_status()

        try:
            faults(primary)
            faults(fallback)
            infer("primary")
            print("PASS: primary inference", flush=True)

            faults(primary, unavailable=True)
            for _ in range(3):
                infer("fallback")
            status = client.get(f"{gateway}/status")
            status.raise_for_status()
            if status.json()["backends"]["primary"]["state"] != "open":
                raise RuntimeError("Primary circuit did not open after three failures")
            print("PASS: fallback inference and open primary circuit", flush=True)

            faults(primary)
            # Compose defaults to a 10-second circuit recovery timeout.
            time.sleep(10.1)
            infer("primary")
            print("PASS: primary recovery", flush=True)

            faults(primary, delay_seconds=2)
            infer("fallback")
            print("PASS: timeout failover", flush=True)

            faults(primary, unavailable=True)
            faults(fallback, unavailable=True)
            response = client.post(f"{gateway}/v1/infer", json=PAYLOAD)
            if response.status_code != 503:
                raise RuntimeError(f"Expected all-down 503, received {response.status_code}")
            print("PASS: 503 when both backends fail", flush=True)

            metrics = client.get(f"{gateway}/metrics")
            metrics.raise_for_status()
            if "bront_backend_attempts_total" not in metrics.text:
                raise RuntimeError("Backend attempt metrics are missing")
            print("PASS: Prometheus metrics endpoint", flush=True)
        finally:
            # Attempt both resets even when one service is unreachable.
            try:
                faults(primary)
            finally:
                faults(fallback)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default=GATEWAY)
    parser.add_argument("--primary-url", default=PRIMARY)
    parser.add_argument("--fallback-url", default=FALLBACK)
    args = parser.parse_args()
    main(args.gateway_url, args.primary_url, args.fallback_url)
