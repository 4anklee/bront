# Local Kubernetes with Triton

This deployment runs two gateway replicas, one CPU Triton server, and one mock fallback
in the `bront` namespace. All three have ClusterIP Services, CPU/memory
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
kind create cluster --name bront \
  --config deploy/kubernetes/kind.yaml --wait 120s
docker build -t bront:local .
docker pull nvcr.io/nvidia/tritonserver:26.08-py3
uv run --frozen python scripts/load_kind_images.py \
  bront:local nvcr.io/nvidia/tritonserver:26.08-py3
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
kubectl --context kind-bront apply -k deploy/kubernetes
kubectl --context kind-bront -n bront \
  rollout status deployment/triton --timeout=300s
kubectl --context kind-bront -n bront \
  rollout status deployment/fallback --timeout=120s
kubectl --context kind-bront -n bront \
  rollout status deployment/gateway --timeout=120s
kubectl --context kind-bront -n bront get pods,services

# Leave this running; the port is separate from the Compose demo ports.
kubectl --context kind-bront -n bront \
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
kubectl --context kind-bront -n bront \
  scale deployment/triton --replicas=1
```

Run the generic gateway suite through the port-forward:

```sh
uv run bront run examples/gateway/suite.toml \
  --url http://127.0.0.1:28000/v1/infer --output reports/kubernetes-baseline.json
```

A port-forward selects one pod. It is a functional check, not a Service load
balancing or cluster-performance measurement.

## Run through the gateway Service

The optional Job runs the same Bront suite engine inside Kubernetes against
`http://gateway:8000`. It dispatches 1,000 measured requests with concurrency 20,
plus 20 warmup requests. Keep-alive connections can remain attached to one pod;
an even distribution is not guaranteed.

```sh
kubectl --context kind-bront -n bront create configmap bront-run-suite \
  --from-file=suite.toml=examples/kubernetes/suite.toml \
  --from-file=cases.jsonl=examples/gateway/cases.jsonl \
  --dry-run=client -o yaml | kubectl --context kind-bront -n bront apply -f -
run_job=$(kubectl --context kind-bront create -f deploy/kubernetes/run-job.yaml \
  -o jsonpath='{.metadata.name}')
kubectl --context kind-bront -n bront wait \
  --for=condition=complete "job/$run_job" --timeout=180s
mkdir -p reports
kubectl --context kind-bront -n bront logs "job/$run_job" > "reports/$run_job.json"
uv run bront report "reports/$run_job.json" --output "reports/$run_job.html"
```

The Job is excluded from the application's Kustomization and is never launched by
applying the reference deployment. It uses the already loaded `bront:local` image.
Logs contain the full schema-3 JSON, including request samples, so the report can
be rendered and checked without a custom summary schema. The count is intentionally
small to stay below ordinary container log rotation limits. Do not use this log
export pattern for unbounded or large sustained runs; provide persistent artifact
storage for those workloads.

If the Job fails its assertions it remains failed and is not retried. Export logs
even when `kubectl wait` fails; inspect Job diagnostics if the container could not
start or the logs are not a valid artifact. Finished Jobs expire after one hour.
Use `kubectl --context kind-bront -n bront delete "job/$run_job"` for earlier cleanup.

On an otherwise idle stack, compare each gateway pod's `bront_requests_total`
counter before and after the run to confirm both received traffic. Counters are
per-process and reset on pod replacement; scraping `/metrics` does not increment
the request counter. The [dashboard](observability.md) preserves pod identity.

## Verify rollout and outage scenarios

Use the reusable suites rather than a gateway-specific orchestration script.
The API-server Service proxy keeps the traffic endpoint available while pods are
replaced; it adds overhead and is not a direct in-cluster performance measurement.
Leave this running in a separate terminal:

```sh
kubectl --context kind-bront proxy --port 18080
```

Then run one experiment at a time:

```sh
uv run bront run examples/scenarios/kubernetes-restart.toml \
  --output reports/restart.json
uv run bront run examples/scenarios/kubernetes-scale-zero.toml \
  --output reports/outage.json
uv run bront report reports/restart.json --output reports/restart.html
```

These are separate experiments: gateway replacement under traffic, and a Triton
outage with restoration. They do not claim to combine an outage and restart in
one suite. The [scenario guide](scenarios.md) documents ownership guards, observed
replacement, fault overlap, and recovery semantics. Custom suite timing must leave
enough time for the local cluster to reconcile and restore the workload.

The gateway rollout keeps `maxUnavailable: 0` and `maxSurge: 1`, and requires five
seconds of readiness before retiring a previous pod. Its `preStop` hook creates
the pod-local `BRONT_DRAIN_FILE` marker, then waits ten seconds. During that window,
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
only for the tiny demo model; larger models need a different distribution
mechanism, such as an image or a volume. The Kubernetes deployment does not load
your local `.env` file.

## Updates, troubleshooting, and cleanup

For application code changes, rebuild and reload the local image, then restart
the application Deployments. Reusing a tag alone does not trigger a rollout:

```sh
docker build -t bront:local .
uv run --frozen python scripts/load_kind_images.py bront:local
kubectl --context kind-bront apply -k deploy/kubernetes
kubectl --context kind-bront -n bront \
  rollout restart deployment/gateway deployment/fallback
```

Restart the port-forward after the gateway pod is replaced. To investigate a
pod that is not ready:

```sh
kubectl --context kind-bront -n bront get pods
kubectl --context kind-bront -n bront describe pods
kubectl --context kind-bront -n bront logs deployment/triton
kubectl --context kind-bront -n bront logs deployment/gateway
```

Stop the port-forward with Ctrl-C. Remove just the demo namespace, or delete the
entire dedicated cluster when finished:

```sh
kubectl --context kind-bront delete namespace bront
# Alternatively:
kind delete cluster --name bront
```

## Observe individual gateway replicas

Install the optional [Prometheus and Grafana stack](observability.md) to scrape
each gateway pod directly and compare its circuit state and request metrics.
