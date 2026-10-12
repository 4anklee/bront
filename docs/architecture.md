# Architecture and next steps

## Current behavior

The gateway owns one pooled asynchronous HTTP client for its process lifetime,
using FastAPI's [lifespan API](https://fastapi.tiangolo.com/advanced/events/) and
[HTTPX async requests](https://www.python-httpx.org/async/).
Each validated inference request tries primary, then fallback. Backend HTTP
errors, transport errors, timeouts, invalid JSON, and incompatible response shapes
count as failures. A successful response resets that backend's failure count.
There is at most one attempt per backend per request.

Each backend has an independent circuit breaker. After the configured number of
consecutive failed completions it opens. After cooldown, one incoming request is
allowed to probe it; other concurrent requests skip it until the probe completes.
Success closes the circuit; failure starts a new cooldown. Recovery is driven by
traffic, so `/status` can still say `open` after cooldown until a request arrives.
Generation tickets keep older in-flight results from overriding a newer circuit.

The timeout bounds a full backend attempt, including pool wait and reading the
response. A request can spend approximately twice this timeout across two backends.
An upstream timeout cannot guarantee that the backend stopped computing. Failover
is appropriate here because the workload has no side effects.

Metrics report validated request outcomes, actual backend attempt outcomes, and
attempt latency including errors. Skipped circuits do not count as attempts.
Validation failures and disconnected/cancelled requests are not included in the
request outcome counter. `/healthz` is liveness only; neither it nor `/status`
guarantees that a backend will serve the next inference request.

## Deliberate starter boundaries

- Circuit state, mock faults, and metrics are in memory per worker. Run one worker
  per service for the demo. Replicas would have independent observations.
- Routing preserves primary preference; it does not rank backends by latency.
- The schema supports one numeric model (`double`), at most 1,024 finite inputs,
  and an equal number of finite outputs. The default stack uses mocks; the Triton
  stack executes an equivalent FP64 ONNX graph on CPU. Neither uses a trained model.
- This is a local development service without authentication, rate limits,
  admission control, streaming, or persistent state. The mock fault API belongs
  only in a development environment.
- The gateway trusts configured backends. Response byte limits and inbound body
  limits should be added before exposing it to untrusted traffic.

## Suggested implementation order

The mock and Triton adapters now share the existing circuit breaker and routing
logic. Triton model version `1` is pinned in both the adapter and model repository
configuration. See the [Triton walkthrough](triton.md) for the wire contract,
model generation, and a real container outage/recovery check.

1. Add a concurrent benchmark CLI reporting measured success rate, fallback
   count, throughput, and latency percentiles. Run baseline, failure, delay, and
   recovery scenarios with reproducible configuration.
2. Add Kubernetes Deployments, Services, resource limits, and separate readiness
   and liveness probes; validate locally with kind. Decide how replica-local
   circuit state should affect readiness without blocking recovery traffic.
3. Add recent latency/error windows and evaluate whether latency-aware routing
   improves measured behavior over primary-first routing.

TensorRT-LLM and NeMo remain optional future integrations. No production or
performance claims are implied by the starter.
