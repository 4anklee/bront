# Terminal controls

The CLI uses [Typer](https://typer.tiangolo.com/). Run `uv run inferenceguard --help`
from the repository root. Start a local Triton stack with its fault-control proxy:

```sh
docker compose -f compose.triton.yaml -f compose.benchmark.yaml up --build --wait
uv run inferenceguard status
uv run inferenceguard status --check-backends
```

`status` reads readiness and `/status` from **one gateway instance**. It displays
the instance name, observed circuit state, and consecutive failures. A closed
circuit is not a health guarantee; an open circuit may be waiting for recovery
traffic. Readiness is independent of backend health. Through a Kubernetes Service,
successive status calls may observe different pods; use the
[dashboard](observability.md) for simultaneous per-pod observations.

`--check-backends` sends a direct `double([1,2,3])` inference to each configured
backend, using the gateway's primary protocol and validating `[2,4,6]`. It does
not update the gateway's breakers. It **bypasses the gateway and its Toxiproxy
route** unless that proxy is explicitly part of the probe URL. Consequently,
a healthy direct Triton check can coexist with an open primary circuit during
a proxy outage. This is useful evidence of a routing-path fault.

```sh
uv run inferenceguard fault outage
# Send gateway traffic: responses should switch to fallback.
curl -s http://localhost:8000/v1/infer -H 'Content-Type: application/json' \
  -d '{"model":"double","inputs":[1,2,3]}'
uv run inferenceguard status --check-backends
uv run inferenceguard fault reset
# Keep sending traffic through at least one circuit cooldown to observe recovery.

uv run inferenceguard fault delay --latency-ms 2000
# Responses should use fallback after the gateway's primary timeout.
uv run inferenceguard fault reset
```

Faults persist after the CLI exits. Injection requires a clean, enabled proxy;
reset re-enables the primary proxy and removes only the named
`inferenceguard-demo-latency` toxic. It leaves unrelated toxics intact and can be
repeated, including after an injection whose control response was lost. Run one
demo or benchmark at a time. The benchmark still uses unique temporary toxics
and restores its own faults automatically; persistent demo controls are separate.
These fault commands target the **Compose Toxiproxy overlay**, not Kubernetes
Deployments. Kubernetes outage experiments use `kubectl scale` as documented.

## URLs and automation

Commands load the local `.env` without overriding exported environment variables.
Explicit URL options win. `status --gateway-url` and `fault --toxiproxy-url`
use the benchmark's existing defaults (`IG_BENCHMARK_GATEWAY_URL` and
`IG_BENCHMARK_TOXIPROXY_URL`, otherwise localhost at `IG_GATEWAY_PORT`/8000 and
`IG_TOXIPROXY_PORT`/18474). Active checks use `--primary-url`/`--fallback-url`,
then `IG_STATUS_PRIMARY_URL`/`IG_STATUS_FALLBACK_URL`, then localhost at
`IG_PRIMARY_PORT`/8001 and `IG_FALLBACK_PORT`/8002. The gateway's internal
`IG_PRIMARY_URL` may be a container-only DNS name, so it is not used by host probes.

For the alternate host ports used in the walkthrough:

```sh
uv run inferenceguard status --gateway-url http://localhost:18000 \
  --check-backends --primary-url http://localhost:18001 \
  --fallback-url http://localhost:18002 --json
uv run inferenceguard fault outage --toxiproxy-url http://localhost:18474
uv run inferenceguard fault reset --toxiproxy-url http://localhost:18474
```

`status --json` returns separate `backends` observations and `active_checks`
(empty when not requested). Exit 0 means the gateway was ready and all requested
probes passed; it does not certify unprobed backends. Exit 1 means not ready or
an active check failed; exit 2 means invalid options, an unreachable gateway, or
a control error. Fault control exits 0 on success and 2 on failure. `--timeout`
bounds status reads and each active backend check separately.
