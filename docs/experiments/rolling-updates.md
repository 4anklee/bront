# Rolling updates with Triton healthy and unavailable

Measured on **2026-10-12**, starting `2026-10-12T23:23:49.408442+00:00`.
Application source: `98c8a404a626797128da96cb5c10e08562c5eecc`.
[Machine-readable evidence](rolling-updates.json) includes the gateway Deployment
specification, image IDs, pod UIDs before/after, rollout timestamps, per-second
outcomes, client configuration, and environment details. This is one local run
per condition, not a production availability or capacity estimate.

## Question and method

Does replacing **both gateway replicas** interrupt correct inference responses
while the primary is healthy, or while all requests must use fallback?

The driver creates a Job inside Kubernetes, sending HTTP traffic to
`http://gateway:8000` through the gateway Service. It uses twenty concurrent
workers and persistent HTTP connections with no client retries. After twenty
unmeasured warmup requests, each run schedules requests for sixty seconds and
drains the remaining in-flight requests. Every successful response must satisfy
`double([1,2,3]) == [2,4,6]` and report the expected backend.

Five seconds into measured traffic, the driver restarts the gateway Deployment.
It verifies that two new pod UIDs replace the two original pods and that the
rollout finishes before measured traffic ends. It then repeats with Triton
scaled to zero and waits for its pod to be deleted before fallback warmup.
Finally it restores Triton and verifies primary routing on each gateway pod.

```sh
uv run python scripts/verify_rolling_updates.py \
  --context kind-inference-guard --namespace inference-guard \
  --duration 60 --concurrency 20 --output reports/rolling-updates.json
```

For a fresh reproduction, follow [the cluster setup](../kubernetes.md) and install
[monitoring](../observability.md). Load the same application build under both
`inference-guard:local` and `inference-guard:benchmark`, then restart the gateway
to use that image before running the command. The script restores Triton's
original replica count in `finally` and deletes the benchmark Jobs it creates.
Ordinary generated reports remain ignored; the JSON here is explicitly curated.

## Environment and configuration

- Apple M3 Pro, ARM64, 12 logical CPUs, 18 GiB host RAM; macOS 26.6.2.
- Docker Engine 29.8.2; Linux VM exposes 12 CPUs and
  7.75 GiB RAM. Single kind node, Kubernetes
  v1.37.0, kind v0.33.0, containerd 2.3.4.
- Python 3.14.6 on the host; Python 3.14.8 in the application image.
  Locked application dependencies are in `uv.lock` at the source revision above.
- Two gateway pods, one Uvicorn worker each. Each requests 100m CPU / 128 MiB and
  is limited to 1 CPU / 256 MiB. Each owns independent circuit state.
- Triton `26.08-py3`, one CPU ONNX `double` model instance, pinned model version 1;
  requests 1 CPU / 1 GiB, limits 2 CPU / 2 GiB. The Python fallback requests
  100m CPU / 128 MiB, limits 1 CPU / 256 MiB. Neither model is a trained network.
- One-second total backend attempt timeout; three consecutive failures open a
  circuit; ten-second recovery cooldown. The client uses a five-second deadline.
- RollingUpdate: `maxUnavailable: 0`, `maxSurge: 1`, five-second minimum readiness,
  thirty-second termination grace. A preStop marker makes the pod unready and
  retires HTTP keep-alive connections for ten seconds before shutdown.
- Prometheus and Grafana were running, with five-second per-pod scrapes. The
  separate Compose demo stack was idle. Workloads share the Docker VM; CPU is
  not isolated. The video was recorded after this experiment, separately.

## Measured results

| Expected backend | Correct / attempted | Failed | Elapsed s | Requests/s | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| primary | 61,471 / 61,471 | 0 | 60.025 | 1,024.1 | 14.81 | 47.15 | 70.71 |
| fallback | 61,042 / 61,042 | 0 | 60.023 | 1,017.0 | 14.88 | 47.22 | 71.45 |

Both runs replaced both original gateway pods, completed the rollout during
measured traffic, and met all output/backend checks. No transport errors, HTTP
errors, timeouts, or incorrect outputs were observed. Latency includes all measured
requests and excludes warmup and recovery probes; percentiles use nearest rank.
Elapsed time includes draining in-flight requests after the sixty-second window.

After restoring Triton, both gateway pods returned correct primary results in
**8.001 seconds**. This timer starts before the scale-up command and
includes pod/model startup and sequential checks of the gateways. It is not the
same as the dashboard's open-to-closed episode duration or the video's timing
from proxy reset acknowledgement.

## What this establishes

This run demonstrates continuity for the tested local topology during an orderly
rolling restart, including when fallback carries all traffic. The pod drain marker
and connection retirement were necessary in earlier development checks; a delay
alone had left a small number of transport failures. This artifact reports the
final configuration's fresh run, not a controlled statistical comparison.

It does not establish behavior for abrupt node loss, exhausted fallback capacity,
long-running model requests, cluster upgrades, or production load. A trivial CPU
model and one shared VM cannot support general inference performance claims.
The benchmark's throughput is workload- and resource-specific.
