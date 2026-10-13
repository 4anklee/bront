"""Verify local Kubernetes primary, fallback, readiness, and recovery.

Temporarily scales the Triton Deployment to zero, then restores its replica count.
Start a gateway port-forward before running this script.
"""

import argparse
import json
import subprocess
import time

import httpx


def main(gateway: str, context: str, namespace: str) -> None:
    kubectl = ["kubectl", "--context", context, "--namespace", namespace]

    def control(*args: str) -> str:
        return subprocess.check_output([*kubectl, *args], text=True, timeout=330)

    deployment = json.loads(control("get", "deployment/triton", "-o", "json"))
    replicas = deployment["spec"]["replicas"]
    if replicas != 1:
        raise RuntimeError("This local smoke check expects one Triton replica")
    control("rollout", "status", "deployment/triton", "--timeout=300s")
    control("rollout", "status", "deployment/fallback", "--timeout=120s")
    control("rollout", "status", "deployment/gateway", "--timeout=120s")

    with httpx.Client(base_url=gateway, timeout=5, trust_env=False) as client:

        def infer(backend: str) -> None:
            for values in ([1, 2, 3], [0.1, -2.25, 0.0, 1e100, 1e-100]):
                response = client.post("/v1/infer", json={"model": "double", "inputs": values})
                response.raise_for_status()
                expected = {
                    "model": "double",
                    "outputs": [value * 2 for value in values],
                    "backend": backend,
                }
                if response.json() != expected:
                    raise RuntimeError(f"Unexpected inference response: {response.text}")

        def recovered() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                response = client.post("/v1/infer", json={"inputs": [1, 2, 3]})
                response.raise_for_status()
                if response.json() == {
                    "model": "double",
                    "outputs": [2, 4, 6],
                    "backend": "primary",
                }:
                    return
                time.sleep(0.5)
            raise RuntimeError("Gateway did not return to primary within 30 seconds")

        recovered()
        infer("primary")
        print("PASS: Kubernetes gateway serves Triton inference", flush=True)
        try:
            control("scale", "deployment/triton", "--replicas=0")
            control("wait", "--for=delete", "pod", "-l", "app=triton", "--timeout=120s")
            # Enough requests to open the primary circuit with this deployment's settings.
            settings = client.get("/status")
            settings.raise_for_status()
            for _ in range(settings.json()["configuration"]["failure_threshold"]):
                infer("fallback")
            status = client.get("/status")
            status.raise_for_status()
            if status.json()["backends"]["primary"]["state"] != "open":
                raise RuntimeError("Primary circuit did not open")
            client.get("/readyz").raise_for_status()
            control("wait", "--for=condition=Ready", "pod", "-l", "app=gateway", "--timeout=30s")
            print("PASS: compatible fallback; gateway remains ready during outage", flush=True)
        finally:
            control("scale", "deployment/triton", f"--replicas={replicas}")
            control("rollout", "status", "deployment/triton", "--timeout=300s")
        recovered()
        infer("primary")
        print("PASS: primary routing recovers after Triton returns", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:28000")
    parser.add_argument("--context", default="kind-bront")
    parser.add_argument("--namespace", default="bront")
    args = parser.parse_args()
    main(args.gateway_url, args.context, args.namespace)
