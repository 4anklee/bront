# Local Kubernetes with Triton

This deployment runs two gateway replicas, one CPU Triton server, and one mock fallback
in the `inference-guard` namespace. All three have ClusterIP Services, CPU/memory
requests and limits, and startup, liveness, and readiness probes. Gateway
configuration is generated from `deploy/kubernetes/kustomization.yaml`.

## Create the cluster and load images

Start Docker Desktop. Install [kind](https://kind.sigs.k8s.io/docs/user/quick-start/)
and `kubectl` if missing (`brew install kind kubectl` on macOS). The local setup
uses kind 0.33.0 and Kubernetes 1.37.0. Allocate at least 4 CPUs and 8 GiB of memory
to Docker Desktop as a starting point. Triton's full image is large; allow space
for both Docker's copy and the kind node's copy.

Run from the repository root:

```sh
kind create cluster --name inference-guard \
  --config deploy/kubernetes/kind.yaml --wait 120s
docker build -t inference-guard:local .
docker pull nvcr.io/nvidia/tritonserver:26.08-py3
uv run --frozen python scripts/load_kind_images.py \
  inference-guard:local nvcr.io/nvidia/tritonserver:26.08-py3
```

The explicit `local` image tag and `IfNotPresent` policy let Kubernetes use the
image loaded into kind. No registry push or GPU is required. Importing Triton's
image may take several minutes. The loader creates a temporary archive for the
local image's platform and removes it afterward. This avoids missing-digest
errors when Docker Desktop caches only one platform of a multi-platform image.
It requires Docker API 1.48 or newer (`docker image save --platform`); use a
current Docker Desktop installation. Allow additional temporary disk space for
the exported archive.

## Deploy and connect

```sh
kubectl --context kind-inference-guard apply -k deploy/kubernetes
kubectl --context kind-inference-guard -n inference-guard \
  rollout status deployment/triton --timeout=300s
kubectl --context kind-inference-guard -n inference-guard \
  rollout status deployment/fallback --timeout=120s
kubectl --context kind-inference-guard -n inference-guard \
  rollout status deployment/gateway --timeout=120s
kubectl --context kind-inference-guard -n inference-guard get pods,services

# Leave this running; the port is separate from the Compose demo ports.
kubectl --context kind-inference-guard -n inference-guard \
  port-forward service/gateway 28000:8000 --address 127.0.0.1
```

In another terminal:

```sh
curl http://127.0.0.1:28000/v1/infer \
  -H 'Content-Type: application/json' \
  -d '{"model":"double","inputs":[1,2,3]}'
# {"model":"double","outputs":[2.0,4.0,6.0],"backend":"primary"}

uv run --frozen python scripts/smoke_kubernetes.py
```

The smoke check verifies Triton results, scales Triton to zero, verifies identical
fallback results and gateway readiness, then restores Triton in a `finally` block
and verifies return to primary. Run it against this disposable local deployment.
If the process is forcibly killed, restore with:

```sh
kubectl --context kind-inference-guard -n inference-guard \
  scale deployment/triton --replicas=1
```

You can also run a baseline benchmark through the port-forward:

```sh
uv run --frozen inferenceguard benchmark --scenario baseline \
  --gateway-url http://127.0.0.1:28000 --requests 100 --concurrency 10 \
  --output reports/kubernetes-baseline.json
```

The other CLI scenarios require Toxiproxy, which is only configured in the
Compose benchmark overlay for now. Port-forwarding adds overhead, so these
measurements are a functional check rather than a cluster performance estimate.

To sustain baseline traffic for a minute, replace `--requests 100` with
`--duration 60`. This exercises the pod selected by the port-forward. To test
Service load balancing across replicas, run the benchmark inside the cluster
against `http://gateway:8000` instead.

## Benchmark through the gateway Service

The optional benchmark Job runs inside the cluster and sends 60 seconds of
baseline traffic to `http://gateway:8000` with concurrency 20. It is excluded
from the application Kustomization, so deploying the app does not start a load
test. Build its separate image tag to include the latest CLI without restarting
the gateway pods:

```sh
docker build -t inference-guard:benchmark .
uv run --frozen python scripts/load_kind_images.py inference-guard:benchmark
benchmark_job=$(kubectl --context kind-inference-guard \
  create -f deploy/kubernetes/benchmark-job.yaml -o jsonpath='{.metadata.name}')
kubectl --context kind-inference-guard -n inference-guard \
  wait --for=condition=complete "job/$benchmark_job" --timeout=180s
mkdir -p reports
kubectl --context kind-inference-guard -n inference-guard \
  logs "job/$benchmark_job" > "reports/$benchmark_job.json"
```

Successful Job logs contain a compact JSON summary with counts, latency, errors,
and configuration. Raw samples are omitted (`samples_omitted: true`) to keep
container log rotation from truncating large reports. On failure, logs also include CLI
diagnostics; inspect the Job and its logs before treating them as a JSON report.
Each invocation creates a new Job. Failed benchmarks are not retried, and finished
Jobs are automatically removed after an hour, so export results before then.

The Service selects a gateway pod for each new connection. HTTP keep-alive can
send many requests over the same connection, so an even split is not guaranteed.
To verify both replicas handle traffic, compare each pod's
`inferenceguard_requests_total` metrics before and after the run (use `kubectl
exec` or separate pod port-forwards to fetch `/metrics`). On an otherwise idle
stack, the increases sum to the measured request count plus warmup and preparation
requests. Scraping `/metrics` does not increment that counter. This check assumes
the pods are not replaced during the run; rolling-update measurements need to
account for counters from both old and new pods.

To inspect or remove a Job manually:

```sh
kubectl --context kind-inference-guard -n inference-guard describe "job/$benchmark_job"
kubectl --context kind-inference-guard -n inference-guard logs "job/$benchmark_job"
kubectl --context kind-inference-guard -n inference-guard delete "job/$benchmark_job"
```

## Verify rolling updates under traffic

After loading the current `inference-guard:benchmark` image as above, run:

```sh
uv run --frozen python scripts/verify_rolling_updates.py \
  --duration 60 --concurrency 20 --output reports/rolling-updates.json
```

This check creates temporary in-cluster Jobs using the benchmark's existing
request validator and bounded-concurrency load generator. It runs two experiments:

1. Sustain traffic through `http://gateway:8000`, restart the gateway Deployment,
   and require all measured requests to succeed through Triton.
2. Scale Triton to zero, sustain fallback traffic through the same Service,
   restart the gateway again, and require all measured requests to succeed
   through fallback.

Each experiment includes five seconds of measured traffic before the restart.
The check confirms both pod identities changed and that the rollout completed
before the measured traffic ended. Failures are counted without client retries.
The JSON report includes counts, latency, errors, per-second traffic summaries,
up to 100 failed samples, and pod identities and rollout timestamps. Exit code
`1` indicates failed expectations or an incomplete experiment.

Triton is restored in a `finally` block and primary inference is checked directly
on **each** replacement gateway pod. Temporary Jobs are removed after each
experiment. Reported recovery time includes Triton startup and the checks of
both gateway pods. Run against this local deployment without concurrent deployments or
fault injection. If the script is forcibly killed, restore Triton with the scale
command above; temporary Jobs expire automatically after an hour. Gateway pods
are replaced, so restart any existing port-forward afterward.

The gateway rollout keeps `maxUnavailable: 0` and `maxSurge: 1`, and requires five
seconds of readiness before retiring a previous pod. Its `preStop` hook creates
the pod-local `IG_DRAIN_FILE` marker, then waits ten seconds. During that window,
readiness returns 503 and responses include `Connection: close`, so clients
finish their requests and reconnect through the Service. The process keeps
serving late arrivals while endpoint removal propagates. The remaining portion
of the 30-second termination grace period allows requests to finish. These settings
are checked with a local workload; a passing run is not a guarantee for every
network, request duration, or load level.

## Configuration and health

| Component | CPU request / limit | Memory request / limit | Readiness |
| --- | --- | --- | --- |
| Gateway | 100m / 1 | 128 MiB / 256 MiB | `/readyz`: initialized and not draining |
| Triton | 1 / 2 | 1 GiB / 2 GiB | Pinned `double` model version `1` is ready |
| Fallback | 100m / 1 | 128 MiB / 256 MiB | `/healthz`: mock HTTP service is running |

These are initial budgets for the tiny demo model. Tune them using observed usage
before changing the workload. Triton uses a `Recreate` update strategy to avoid
temporarily running two large instances locally; the gateway handles that gap
through fallback.

Gateway liveness uses `/healthz`. Readiness uses `/readyz`, which returns 503
outside the initialized application lifespan or when the pod is draining.
Backend failures do not change
gateway readiness: keeping the gateway reachable allows fallback responses and
traffic-driven circuit recovery. If both backends fail, inference returns 503
while the gateway stays ready. Readiness is not a guarantee of inference success.
See [Kubernetes probe semantics](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/).

Each gateway pod runs one process. Circuit state and metrics are local to each
process, so the two replicas observe backend health and recover independently.
`/status` describes only the pod that handles the request; it is not a combined
view of both replicas. The gateway Service routes to both ready pods, while a
port-forward selects just one pod.

Kustomize packages the existing tiny ONNX model and configuration into a ConfigMap
mounted under `/models/double`. Generated names include content hashes, so config
or model changes trigger a Deployment update on the next apply. This approach is
only for the 140-byte demo model; larger models need a different distribution
mechanism, such as an image or a volume. The Kubernetes deployment does not load
your local `.env` file.

## Updates, troubleshooting, and cleanup

For application code changes, rebuild and reload the local image, then restart
the application Deployments. Reusing a tag alone does not trigger a rollout:

```sh
docker build -t inference-guard:local .
uv run --frozen python scripts/load_kind_images.py inference-guard:local
kubectl --context kind-inference-guard apply -k deploy/kubernetes
kubectl --context kind-inference-guard -n inference-guard \
  rollout restart deployment/gateway deployment/fallback
```

Restart the port-forward after the gateway pod is replaced. To investigate a
pod that is not ready:

```sh
kubectl --context kind-inference-guard -n inference-guard get pods
kubectl --context kind-inference-guard -n inference-guard describe pods
kubectl --context kind-inference-guard -n inference-guard logs deployment/triton
kubectl --context kind-inference-guard -n inference-guard logs deployment/gateway
```

Stop the port-forward with Ctrl-C. Remove just the demo namespace, or delete the
entire dedicated cluster when finished:

```sh
kubectl --context kind-inference-guard delete namespace inference-guard
# Alternatively:
kind delete cluster --name inference-guard
```
