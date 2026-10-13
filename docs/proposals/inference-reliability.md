# Bront roadmap

Bront validates inference releases by running repeatable experiments against
existing services. The package, CLI, and distribution now use the Bront name.

## Implemented

1. **External endpoints.** Typed TOML suites, JSONL cases, generic HTTP JSON
   targets, output validation, deadlines, bounded concurrency, and partial reports.
2. **Reusable scenarios.** Proxy outage/delay, Kubernetes restart/scale-zero,
   lifecycle timing, restoration, and service/preferred-backend recovery checks.
3. **Release comparisons.** Explicit assertions, provenance, repeated runs,
   compatibility policies, and conservative observed-range regression checks.
4. **Reviewable reports.** Offline HTML, per-window traffic, scenario timelines,
   compatibility differences, and embedded JSON evidence, uploaded as CI artifacts.
5. **Naming and cleanup.** `bront` distribution/import/CLI, optional reference
   fixtures, and removal of the superseded gateway-specific experiment commands.

The core workflow is implemented. This establishes a working MVP, not validated
external demand or a production-performance claim. See the [architecture](../architecture.md)
and [migration guide](../migration.md) for the current structure.

## Next: realistic inference and external validation

- Add a small trained CPU model served by Triton, with reproducible preprocessing,
  model version, input cases, and expected outputs. Retain `double` for rapid smoke
  checks. Any fallback must use an equivalent model and explicit tolerances.
- Publish a reproducible release experiment with commands, configuration, hardware,
  measured results, and limitations of the conclusion.
- Have at least two external users test services they already maintain, without
  adopting Bront's reference gateway. Address their setup and evidence-review friction.
- Publish an initial package release after those checks. Package naming in this
  repository does not reserve or publish a registry name.

## Deliberately deferred

JUnit export, a hosted service, a custom Kubernetes operator, adaptive routing,
streaming/LLM metrics, and causal diagnosis are outside the current milestone.
The runner generates closed-loop traffic: its arrival rate falls when requests
slow down. Fixed-rate overload testing needs a different load contract or a
specialized load generator. Repeated observed ranges are not significance tests.

Use optional Prometheus/Grafana evidence for per-pod investigation. The reference
stack stays a test fixture, not infrastructure that users must install.
