# Release assertions and baseline/candidate comparisons

`bront compare` checks whether repeated baseline and candidate runs are compatible,
complete, and sufficient for a declared release policy. It returns a JSON artifact
and an exit code for CI. `bront run --repeat N` collects the runs and writes a
manifest referencing their individual reports.

A passing comparison means the supplied evidence meets the declared checks.
It does not establish general model quality, statistical significance, or a causal
explanation for a performance change.

## Record the experiment

Start with [the example suite](../examples/comparison/baseline.toml), or extend an
existing suite. The request cases and scenario configuration remain the same as
in the [endpoint](external-endpoints.md) and [scenario](scenarios.md) guides.

Add deployment metadata:

```toml
[metadata]
model_revision = "model-sha-or-immutable-version"
serving_revision = "image-digest-or-serving-commit"
architecture = "arm64"

[metadata.resources]
cpu = "2 vCPU; Apple M-series host"
memory = "2Gi"
accelerator = "none"

[metadata.labels]
environment = "local-staging"
hardware = "identify-your-test-host-or-instance-type"
```

Describe the **target's** actual configuration. Replace example values, and record
limits or shared-host conditions explicitly. Resources and labels are free-form
string maps; include relevant CPU/GPU models, quantities, memory, and configuration.
Model revision, serving revision, architecture, and a nonempty resources map are
required for a comparison verdict. Policies can also require named labels.

This metadata is caller-declared, not discovered or attested by querying the
service. Missing fields remain visible as `provenance.missing_metadata`; a baseline
run may still pass its own response checks, but a comparison cannot pass without
the required context. The runner records its own architecture, OS, Python and
library versions separately; it never uses those as guesses about a remote target.
Metadata is included in artifacts. Use shareable identifiers, not credentials.

Keep explicit absolute limits in each run's suite:

```toml
[assertions]
max_failed_requests = 0
max_validation_failures = 0
max_error_rate = 0
max_p95_ms = 200
max_p99_ms = 500
```

The validation-failure limit defaults to zero, independently of the failed-request
budget. All configured limits apply together; a rate budget does not replace
the failed-request cap. Permitting transport failures therefore does not silently permit incorrect
outputs. `max_error_rate` is a fraction from zero to one. Percentiles include all
measured attempts, including failures; warmup is excluded. Existing service and
preferred-backend recovery deadlines remain available for reversible scenarios.

## Collect repeated runs

Run a baseline against the current deployment:

```sh
uv run bront run baseline.toml --repeat 3 --output reports/baseline.json
```

Create `candidate.toml` with the same workload, cases, checks, and metadata except
for the intentionally changed serving revision. After deploying that candidate:

```sh
uv run bront run candidate.toml --repeat 3 \
  --url http://candidate:9000/predict --output reports/candidate.json
```

Each repetition creates a fresh HTTP client and repeats warmup and the selected
scenario. Runs are sequential. A completed assertion failure is retained and does
not stop subsequent repetitions. Execution errors stop the series; Ctrl-C saves
both the interrupted child and the partial manifest. Repeated Kubernetes restart
scenarios intentionally restart the Deployment each time.

With `--repeat 1` (the default), `--output` remains a single run report. With a
larger count it is a `bront.series` manifest; children live in a unique sibling
directory such as `baseline-runs-<id>/001.json`. The manifest is updated after
each child and includes run IDs and canonical JSON SHA-256 digests. Keep the
manifest and its directory together when copying or downloading artifacts.
Reusing an output filename replaces that manifest but leaves previous child
directories available; use distinct filenames to retain convenient references.

## Declare the comparison policy

Save `limits.toml`:

```toml
version = 1
minimum_runs = 3
minimum_requests = 100
minimum_duration_seconds = 0
required_labels = ["environment", "hardware"]
allowed_changes = ["deployment.serving_revision"]

max_p95_increase_percent = 20
max_p99_increase_percent = 25
max_throughput_drop_percent = 10
max_error_rate_increase = 0
```

```sh
uv run bront compare reports/baseline.json reports/candidate.json \
  --policy limits.toml --output reports/comparison.json
```

`--json` prints the same comparison saved to disk. Individual reports can also be
combined with repeated options:

```sh
uv run bront compare baseline-1.json candidate-1.json \
  --baseline-run baseline-2.json --baseline-run baseline-3.json \
  --candidate-run candidate-2.json --candidate-run candidate-3.json \
  --policy limits.toml
```

`minimum_runs` defaults to three and cannot be set below three. Each run must meet
the request and elapsed-duration minima; small runs are not pooled to satisfy them.
The default minimum is 100 requests per run. Duration includes the in-flight drain,
consistent with the run report. A larger request count does not replace repeat
evidence, and three repeats are not a guarantee of statistical power.

The example [limits file](../examples/comparison/limits.toml) leaves relative
performance gates disabled and gates correctness/error counts for shared CI.
Enable performance limits when resource contention and deployment conditions are
controlled. Always choose meaningful per-run absolute limits as well.

## Compatibility and intended changes

New run reports use `report_type: "bront.run"`, schema 3, and contain:

- A canonical contract and fingerprint covering adapter, method, route digest,
  header configuration, backend attribution, input-case digest and order, workload,
  warmup, timeout, assertions, scenario settings, and measurement semantics.
- A unique run ID and runner identity, including a hash of installed package source.
- Declared deployment metadata and an explicit list of missing required fields.

Endpoint origins may differ by default. URL paths and queries are represented by
a digest, so changing a route needs explicit allowance. Literal non-credential
header values are also digested. Known credential headers and environment-backed
headers omit their values from the contract, permitting credential rotation.
If a tenant, routing header, or credential principal changes the experiment, record
that condition in deployment labels. Cases, bodies, expected outputs, and raw URL
paths/queries are not copied into provenance; hashes are not encryption or attestations.

All runs within each group must have identical contracts, runner identities, and
deployment metadata. Mixing serving revisions within the candidate group is
incompatible even if changing serving revision between groups was allowed.

Between groups, the policy may explicitly allow only these changes:

| Policy path | Intended variable |
| --- | --- |
| `deployment.model_revision` | A different model revision under the same tested contract. |
| `deployment.serving_revision` | A serving-image or code revision. |
| `deployment.architecture` | The target architecture. |
| `deployment.resources` or `deployment.resources.cpu` | All resource declarations, or one resource key. |
| `deployment.labels.hardware` | One explicitly named environment label. |
| `contract.route_sha256` | An intentional endpoint path/query change. |
| `contract.scenario.context`, `.namespace`, `.deployment`, or `.proxy` | A different explicitly named control object. |

Every allowed difference remains visible in the artifact. Workload settings,
correctness assertions, input cases, scenario timing, and the runner cannot be
exempted. Runner source hashing is deliberately conservative: even a package
source edit that does not affect timing requires collecting both groups again
with the same runner. Control URLs are omitted; scenario kind, scope names, and
parameters remain recorded.

The reader recomputes phase summaries and assertions from samples, checks complete
count/duration execution and scenario evidence, validates manifest digests, and
rejects duplicate paths/run IDs. A copied report does not count as another repeat.
Old schema-1/2 run reports lack the required contract and produce an inconclusive
comparison; they remain usable as historical artifacts. Malformed, interrupted,
truncated, or internally inconsistent evidence cannot produce a passing comparison.
These checks detect inconsistency, not a malicious author fabricating an entire
self-consistent artifact. Use trusted artifact producers.

## Variation and regression rules

The artifact reports every run's p95, p99, attempted throughput, and failure rate,
plus each metric's median, minimum, and maximum. It does **not** pool samples from
different runs into one percentile. Error counts and failed absolute assertions
remain visible beside latency, so fast failures cannot hide a rejected candidate.

For configured relative performance limits, the comparator evaluates the range
of possible changes using the two groups' observed extrema:

- If even the worst observed pairing meets the limit, the check passes.
- If even the best observed pairing exceeds the limit, the check fails.
- If the range crosses the limit, the check is inconclusive.

For example, a baseline with p95 values `[9, 10, 11]` ms and a candidate with
`[10, 11, 12]` ms has median growth of 10%. Its extrema span about -9% to +33%,
so a 20% regression limit is inconclusive. A consistently 30% slower candidate
fails that limit. These are conservative observed-range checks, not confidence
intervals, hypothesis tests, or a claim that runs are statistically independent.
Control cache state, interference, ordering, and other experimental conditions;
collect more evidence when the result is inconclusive.

Latency increases and throughput drops are percentages. Error-rate increase is
an absolute fraction: `0.01` permits one percentage point. A zero baseline yields
zero relative change only when the candidate is also zero; otherwise a relative
limit is undefined and inconclusive. Absolute candidate assertions still apply.
Metrics with no configured relative limit are descriptive only.

## Verdicts and artifacts

| Status | Exit | Meaning |
| --- | --- | --- |
| `passed` | 0 | Complete compatible evidence meets the run assertions and configured comparison checks. |
| `failed` | 1 | Sufficient compatible evidence includes a candidate assertion failure or a clear regression. |
| `inconclusive` | 2 | Missing/invalid evidence, inadequate repeats/samples/context, a failed baseline, or variation crossing a limit. |
| `incompatible` | 2 | A contract, runner, or metadata difference was not explicitly allowed. |

Configuration and output errors also exit 2. With insufficient evidence, the
comparison remains inconclusive even if an individual candidate run already
failed; the artifact still shows that run's failure. CI must treat both 1 and 2
as non-passing. No incomplete workload passes because its completed samples were fast.

Comparison artifacts identify themselves as `bront.comparison`, schema 1.
Repeated-run manifests use `bront.series`, schema 1. Input limits are 100 runs per
group, 128 MiB per file, and 256 MiB total input. References must stay inside the
manifest's directory tree. Comparison output cannot overwrite its inputs or policy.

CI exercises repeated runs and compatible, insufficient, and incompatible
comparisons against the standalone HTTP fixture. The existing
`external-endpoint-reports` artifact includes manifests, child reports, and
comparison results. Run the same check locally with:

```sh
uv run python scripts/smoke_external.py
```

Render the saved comparison for review:

```sh
uv run bront report reports/comparison.json --output reports/comparison.html
```

The [HTML report](reports.md) preserves the recorded verdict and includes the
comparison JSON. Render the individual runs or series separately for request and
scenario timelines; comparison JSON does not embed source samples.

Keep generated artifacts out of Git. Share the artifact bundle or a curated
experiment containing commands, configuration, hardware, and the measured limits
of the conclusions.
