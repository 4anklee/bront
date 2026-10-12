# Kubernetes observability

Start the [local Kubernetes stack](kubernetes.md), including the latest gateway
image, then install the optional monitoring stack:

```sh
kubectl --context kind-inference-guard apply -k deploy/kubernetes/observability
kubectl --context kind-inference-guard -n inference-guard rollout status deployment/prometheus
kubectl --context kind-inference-guard -n inference-guard rollout status deployment/grafana
```

Run each port-forward in a separate terminal:

```sh
kubectl --context kind-inference-guard -n inference-guard port-forward service/prometheus 19090:9090
kubectl --context kind-inference-guard -n inference-guard port-forward service/grafana 13000:3000
```

Open [the dashboard](http://localhost:13000/d/inferenceguard) (anonymous, read-only)
and [Prometheus targets](http://localhost:19090/targets). Expect **two distinct
healthy gateway pod targets** after discovery. A rollout temporarily adds a third.
Prometheus discovers pod IPs directly, retaining `pod`, `namespace`, and `instance`
labels. It does not scrape the load-balanced gateway Service. Discovery has only
namespace-scoped permission to get/list/watch pods; Grafana has no API token.
Running pods remain scrape targets during draining, including their final metrics.

The provisioned dashboard has a pod selector and eight panels: validated request
success, successful fallback share, requests/second, p95 backend attempt latency,
observed circuit state, last completed recovery, transitions, and scrape health.
Run the [in-cluster benchmark](kubernetes.md#benchmark-through-the-gateway-service)
to populate it. Short/no traffic can produce gaps in rate/ratio panels; gaps are
not evidence of success. Counters and histograms reset when a pod is replaced.

## Metric semantics

- `inferenceguard_circuit_state{backend,state}` is one-hot: exactly one of
  `closed`, `open`, and `half_open` is 1 per backend in each process. It describes
  **observations**, not actively checked backend health. An idle open circuit
  stays open past its cooldown until new traffic probes it.
- `inferenceguard_circuit_transitions_total{backend,from_state,to_state}` captures
  transitions even if a half-open probe completes between five-second scrapes.
  Stale completions from an older circuit generation do not count.
- `inferenceguard_circuit_recovery_duration_seconds` measures first opening to
  successful recovery probe, including the outage, cooldowns, and failed probes.
  `inferenceguard_circuit_last_recovery_seconds` is absent until a recovery
  completes. These **do not measure time since fault removal**; the benchmark's
  recovery phase measures that separately.
- Request success excludes input validation failures and cancelled requests.
  Successful fallback attempts equal requests successfully served by fallback.
  Backend latency includes failed attempts and is not end-to-end latency.

To inspect pod independence, keep traffic running and scale Triton to zero, then
restore it. Always complete the restoration command:

```sh
kubectl --context kind-inference-guard -n inference-guard scale deployment/triton --replicas=0
# Observe fallback traffic and each primary circuit opening.
kubectl --context kind-inference-guard -n inference-guard scale deployment/triton --replicas=1
kubectl --context kind-inference-guard -n inference-guard rollout status deployment/triton
# Continued traffic eventually probes and closes each primary circuit.
```

PromQL checks (the first should be 2 outside a rollout):

```promql
count(up{job="gateway-pods"} == 1)
inferenceguard_circuit_state{backend="primary",state="open"}
increase(inferenceguard_circuit_transitions_total{to_state="closed"}[5m])
inferenceguard_circuit_last_recovery_seconds{backend="primary"}
```

This local stack uses ephemeral storage and six-hour Prometheus retention. Pod
replacement loses monitoring history. Both Services are ClusterIP; the documented
port-forwards bind localhost. Authentication, persistent storage, and alerting
are future deployment work. Remove monitoring with:

```sh
kubectl --context kind-inference-guard delete -k deploy/kubernetes/observability
```

Configuration references: [Prometheus Kubernetes discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config)
and [Grafana file provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
