# Reusable fault and rollout scenarios

Add a `[scenario]` table to a `bront run` suite to coordinate a control action with
continuous HTTP traffic. The target, cases, and response checks remain independent
of the control driver. The same scenario can test a different inference service
without gateway-specific status or health endpoints.

One suite runs one action with a timed workload. Omit `[scenario]` for a baseline.
The four initial drivers are:

| Kind | Action | End condition | Cleanup |
| --- | --- | --- | --- |
| `proxy-outage` | Disable one named Toxiproxy proxy. | Proxy disabled; hold; enable and verify. | Restore enabled state. |
| `proxy-delay` | Add a uniquely named downstream latency toxic. | Toxic observed; hold; remove and verify. | Remove only this run's toxic. |
| `kubernetes-scale-zero` | Scale one named Deployment to zero. | All its pods removed; hold; restore and verify readiness. | Restore its original replicas. |
| `kubernetes-restart` | Change the Deployment's pod-template restart annotation. | All original pods replaced and all desired replicas ready. | Intentional rollout; no rollback. |

A proxy outage leaves the inference process running; scale-to-zero removes its
pods. Treat the results as different experiments. Faulting a primary route while
calling a gateway exercises fallback. Faulting the only route to a standalone
service can produce expected failed requests; declare an appropriate error budget.

## Proxy outage or delay

The traffic path must already pass through the named proxy. Bront controls an
existing proxy, without creating proxies or changing upstream addresses. Control
access must be available separately from the faulted traffic path.

```toml
[workload]
cases = "cases.jsonl"
duration_seconds = 30
concurrency = 4
warmup_requests = 4
request_timeout_seconds = 3

[scenario]
kind = "proxy-outage"
url = "http://127.0.0.1:18474"
proxy = "my-model-route"
at_seconds = 2
hold_seconds = 3
control_timeout_seconds = 10
observe_timeout_seconds = 60
```

For delay, change `kind` to `"proxy-delay"` and add `latency_ms = 1500`. The toxic
has zero jitter, probability one, and affects downstream response traffic.
These operations use the [Toxiproxy HTTP API](https://github.com/Shopify/toxiproxy#http-api).
The initial control adapter uses an unauthenticated HTTP endpoint; target
Authorization headers are never forwarded to it.

To use the existing Compose demonstration, start the
[benchmark overlay](../README.md#benchmark-failover-and-recovery), then:

```sh
uv run bront run examples/scenarios/proxy-outage.toml --output reports/scenarios/outage.json
uv run bront run examples/scenarios/proxy-delay.toml --output reports/scenarios/delay.json
```

Use `--url http://127.0.0.1:18000/v1/infer` if your gateway is published on port
18000. This overrides the inference target only; the control URL stays in TOML.
Run one experiment at a time per proxy. Preflight requires an enabled proxy with
no existing toxics. The driver rechecks its name, listen address, and upstream
before mutation and restoration, and detects unrelated faults during observation.
It never invokes Toxiproxy's global reset API or deletes someone else's toxic.
Toxiproxy has no proxy UID or compare-and-swap update: identical recreation or
concurrent toggles cannot be reliably distinguished. Exclusive use is required.

## Kubernetes rolling restart or backend shutdown

Install `kubectl` on the runner and configure access to the explicit context.
The identity needs Deployment get/patch and namespace-scoped ReplicaSet/pod list
permissions. A server-side dry-run checks the patch and admission before traffic;
Bront does not request broader access or switch the current kubectl context.

```toml
[scenario]
kind = "kubernetes-restart"
context = "kind-inference-guard"
namespace = "inference-guard"
deployment = "gateway"
expected_replicas = 2
at_seconds = 3
control_timeout_seconds = 10
observe_timeout_seconds = 45
```

Use at least 60 seconds of measured traffic for the reference gateway's draining
and replacement process. `kubernetes-scale-zero` also requires `hold_seconds`;
its example targets `triton` with `expected_replicas = 1`, runs for 90 seconds,
and allows 45 seconds per observation. This accommodates Triton's 30-second pod
termination grace period and leaves time for restoration and recovery.

Preflight requires the specified replica count, a fully observed and ready
Deployment, and no paused or previously owned fault. Restart additionally
requires the `RollingUpdate` strategy. Pod attribution follows controller
ownership from Deployment to ReplicaSet to pod, not merely shared labels.

Mutations use [Kubernetes JSON patches](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/)
with tests for UID, resource version, and current spec. The driver records original
replicas and pod UIDs. Scale-to-zero adds a unique `bront.dev/run-id` ownership
annotation, restores replicas only if the object and expected fault spec still
match, and removes that annotation on successful restoration. Concurrent spec or
ownership changes produce an error rather than an overwrite. Concurrent metadata
writes between a read and patch can also cause a conservative conflict.

Rolling restart adds a unique `bront.dev/restart-id` annotation to the pod
template. This intentionally remains after the run, including if traffic fails.
Completion requires updated/ready/available replica counts, controller observation
of the new generation, no terminating old pods, and replacement pod UIDs. A Ready
count alone is insufficient. No automatic rollout undo is attempted.

For a local demonstration that reaches Service endpoints through the Kubernetes
API server, start this in a separate terminal:

```sh
kubectl --context kind-inference-guard proxy --port 18080
```

Then run:

```sh
uv run bront run examples/scenarios/kubernetes-restart.toml --output reports/scenarios/restart.json
uv run bront run examples/scenarios/kubernetes-scale-zero.toml --output reports/scenarios/scale-zero.json
```

Stop `kubectl proxy` with Ctrl-C afterwards. The examples use the API-server
Service proxy, not a pod port-forward. Its extra hop affects latency and
throughput; these are continuity experiments, not serving-performance claims.
For representative measurements, run Bront with `kubectl` and its scoped
credentials inside the cluster and point `target.url` at the inference Service.
The existing [in-cluster rolling verification](kubernetes.md#verify-rolling-updates-under-traffic)
remains available for the reference deployment.

## Timeline and completion

All measured request start times and scenario events share the measured traffic's
monotonic origin (`scenario.clock_origin = "measurement"`). Preflight occurs
before warmup and has negative relative times. If measurement never starts,
`clock_origin` remains `"preflight"` and event times are relative to driver setup.
The report contains:

- `preflight`: control capabilities and initial state checked without mutation.
- `apply`: requested schedule, actual start, and mutation acknowledgement.
- `observed`: the requested fault or replacement state confirmed.
- `restore`: temporary fault restoration requested and acknowledged.
- `restored`: the original service-control state/readiness confirmed.

Each event includes status, actual start/finish, and a sanitized error code if
needed. Event finish is an API acknowledgement for apply/restore; observed and
restored finish times are state confirmations. `hold_seconds` starts **after the
fault is observed**, not at the scheduled action time. Observation can take time,
so leave enough duration for apply, observation, hold, restoration, and traffic
afterwards. Configuration requires `at_seconds + hold_seconds < duration_seconds`,
but does not assume this minimal bound guarantees completion.

The runner requires dispatched requests before the action, during the observed
fault hold (or rolling replacement), and after confirmed restoration/replacement.
It records counts in `scenario.observations.traffic_windows`. If traffic ends or
hits its sample cap before the lifecycle completes, the action is cancelled and
cleanup runs; the experiment is an execution error. Request failures still count
against the workload's assertion policy. A lifecycle completing successfully does
not override incorrect responses.

## Availability and preferred-backend recovery

The report observes the first valid response from a request **started after the
restore acknowledgement**, measured at response completion. This is a service
observation, not an active backend health check. A gateway serving fallback
throughout a fault may have maintained availability the entire time.

Optional policies can separately require a valid response and a particular
backend after restoration:

```toml
[target.backend]
pointer = "/backend"

[assertions]
max_failed_requests = 0
recovery_within_seconds = 3
preferred_backend = "primary"
preferred_backend_within_seconds = 12
```

`recovery_within_seconds` needs no backend identity. Preferred-backend recovery
requires an extraction rule plus an identity actually observed during measured
traffic before applying the fault. Missing configured identity support is rejected
locally; missing observed identity aborts before mutation. A different or unknown
backend after restoration does not satisfy the preferred-backend policy. Unobserved
recovery is recorded as null and fails a configured deadline.

Recovery policies are supported only for reversible fault scenarios. The current
check is a first valid response/first preferred response, not a sustained healthy
window. Broader release-comparison and statistical policies remain a later milestone.

## Cleanup, reports, and CI

Temporary faults are tracked before mutation is sent, so a lost acknowledgement
still triggers restoration. Cleanup also runs after load failures and Ctrl-C.
Control calls and observations have independent timeouts; restoration can extend
past the measured duration. Reports record cleanup as `not_required`, `pending`,
`restored`, `failed`, or `not_reversible`. A cleanup failure is an execution error
(exit 2); Ctrl-C retains exit 130, with any cleanup failure visible in the report.

SIGKILL, repeated forced interruption, unavailable control APIs, and concurrent
changes can prevent cleanup. Inspect the named object and report before another
experiment. Reports retain only configured scope, identifiers, timings, and counts;
they omit control URLs, Kubernetes specs, command output, and credentials. Generated
reports belong in `reports/` or CI artifacts, not Git.

New reports use `report_type: "bront.run"`, `schema_version: 2`, with optional
`scenario` evidence. Schema-1 baseline artifacts remain historical evidence;
this command does not reinterpret them. Demo benchmark schema-v2 reports still
use their separate contract.

CI runs the reusable outage and delay suites against the reference Compose stack
and uploads `scenario-reports`, including failed runs. Unit tests cover control
errors, cancellation, lost acknowledgements, concurrent Kubernetes changes,
replacement evidence, and recovery policy semantics. The local Kubernetes examples
exercise real controller reconciliation and replacement.
