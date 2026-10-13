# Test an external HTTP endpoint

`bront run` sends a repeatable workload to an existing HTTP endpoint and checks
its responses. It does not require the reference gateway, a backend identity,
health endpoints, Docker, or Kubernetes. `bront` is the working product name;
the distribution and import package remain `inference-guard` / `inference_guard`.
The `inferenceguard` command remains available, including the reference demo's
`benchmark`, `status`, and `fault` commands.

## Try the standalone example

From the repository root:

```sh
uv sync --frozen
uv run python examples/http-json/server.py
```

In another terminal:

```sh
uv run bront run examples/http-json/suite.toml --output reports/baseline.json
```

The example sums `features` and returns `prediction`. It deliberately has a
separate contract from the gateway's `double` model. The suite measures 30
requests across three cases after two warmup requests, at concurrency four.
To exercise passing results, incorrect outputs, and an actual interrupted
process together, run `uv run python scripts/smoke_external.py`. CI runs this
check and uploads `external-endpoint-reports` as an artifact.

## Describe your endpoint

Create `suite.toml`:

```toml
version = 1

[target]
adapter = "http-json"
url = "http://localhost:9000/predict"
method = "POST"

# Optional. Environment variables must already be exported in the shell.
# [target.headers]
# Authorization = { env = "MODEL_TOKEN", prefix = "Bearer " }
# X-Experiment = "candidate"

[workload]
cases = "cases.jsonl"
requests = 100
concurrency = 8
warmup_requests = 8
request_timeout_seconds = 5

[assertions]
max_failed_requests = 0
max_p95_ms = 200
```

Create `cases.jsonl` beside it, with one case per line:

```jsonl
{"name":"sum","body":{"features":[1,2,3]},"expect":{"status":200,"json":[{"pointer":"/prediction","equals":6}]}}
{"name":"fraction","body":{"features":[0.1,0.2]},"expect":{"json":[{"pointer":"/prediction","equals":0.3,"abs_tol":0.000001}]}}
```

```sh
uv run bront run suite.toml --output reports/current.json
uv run bront run suite.toml --url http://candidate:9000/predict --output reports/candidate.json
uv run bront run suite.toml --json --output reports/current.json
```

`--url` replaces the complete URL, including the path. JSON output contains the
same report saved to disk. Use `--repeat 3` to collect a manifest of runs, then follow the
[comparison guide](comparisons.md) for compatibility checks and release policies.

Cases are resolved relative to the suite's directory. Names must be unique and
use letters, digits, underscores, dots, or hyphens (up to 64 characters). Cases
are selected round-robin by dispatch index; completion order may differ.
Omitting `body` sends no body; an explicit `"body": null` sends JSON `null`.
JSON bodies get a default `Content-Type: application/json` header unless supplied.
Methods can be GET, POST, PUT, PATCH, DELETE, HEAD, or OPTIONS. Redirects are
recorded as responses and are not followed; requests are not retried.

Headers accept literal strings or `{ env = "NAME", prefix = "..." }` references.
Missing or empty environment variables fail configuration before sending traffic.
`run` does not load `.env` files or inherit HTTP proxy settings. Header names and
ASCII values are checked before traffic; URL userinfo and manual Host or body
framing headers are unsupported.

## Response checks

`expect.status` defaults to 200. An empty or omitted `expect.json` checks only the
HTTP status. Include checks to verify the response body:

| Check | Meaning |
| --- | --- |
| `{"pointer":"/prediction"}` | The value must exist, including if it is null. |
| `{"pointer":"/prediction","equals":null}` | The value must exist and be null. |
| `{"pointer":"/prediction","equals":[1,2]}` | Exact array shape and values. |
| `{"pointer":"/prediction","equals":1.0,"abs_tol":0.001}` | Numeric comparison with absolute tolerance. |
| `{"pointer":"/prediction","equals":100,"rel_tol":0.01}` | Numeric comparison with relative tolerance. |

Paths use [JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901): `""` selects
the entire response, `/predictions/0` selects the first array element, and `~1`
and `~0` escape slash and tilde in object keys. Array indices have no leading
zeros. This is not JSONPath.

Equality is recursive. Numbers compare by value, so `2` equals `2.0`, but booleans
and numeric strings do not equal numbers. Arrays must have the same lengths and
nested shape; objects must have the same keys at the selected path. Tolerances
apply to numeric leaves with
`abs(actual - expected) <= max(abs_tol, rel_tol * max(abs(actual), abs(expected)))`.
Invalid JSON, duplicate object keys, non-finite numbers, missing paths, and
incorrect values fail body checks. These checks establish agreement with the
supplied examples; they do not establish a model's general quality.

Optional backend attribution can read a JSON string or response header:

```toml
[target.backend]
pointer = "/backend"
# Or use header = "X-Backend" instead of pointer.
```

Omit this table when the service does not expose attribution. Missing, non-string,
empty, or oversized identities are recorded as null and do not fail a response.
To require an identity, add an explicit JSON assertion for it.
The [gateway suite](../examples/gateway/suite.toml) demonstrates attribution
without requiring the gateway's `/status` API:

```sh
uv run bront run examples/gateway/suite.toml --output reports/gateway.json
```

## Sustained traffic and verdicts

For a timed run, replace `requests = 100` with `duration_seconds = 60`. These
options are mutually exclusive. If both are absent, the default is 100 requests.
Workers stop dispatching at the deadline, then allow in-flight requests to finish
within their request timeouts. Measured elapsed time and throughput include this
drain. The timeout covers the complete request, including waiting for a connection.

This is **closed-loop traffic**: at most `concurrency` requests are in flight and
a worker sends its next request after completing the previous one. Slow responses
reduce the arrival rate. Results do not establish resilience to a fixed arrival
rate or prove saturation throughput.

Warmup samples are retained separately and excluded from measured latency,
throughput, and assertions. Any failed warmup stops the run with `warmup_failed`;
`max_failed_requests` applies only to the measured phase.

Latency includes successful and failed attempts and uses nearest-rank p50/p95/p99.
Failures are separated into HTTP status, validation, timeout, and transport
categories. Each sample includes its case name, status code, relative start,
duration, optional backend, and sanitized failure detail. Unexpected execution
errors and cancellation cannot produce a passing verdict.

| Exit code | Meaning |
| --- | --- |
| 0 | All declared assertions passed. |
| 1 | A warmup failed or a measured assertion failed. |
| 2 | Configuration, report I/O, execution error, or sample limit reached. |
| 130 | Interrupted with Ctrl-C; inspect the partial report. |

The default policy allows zero failed requests and zero output-validation failures.
Set `max_failed_requests` to a transport/HTTP failure budget; validation failures
remain separately limited by `max_validation_failures`. Optional `max_error_rate`,
`max_p95_ms`, and `max_p99_ms` add absolute release limits.
A response counted as successful passed the specified checks, which may consist
only of status if no JSON assertions were declared.

## Reports and limits

Reports identify themselves with `report_type: "bront.run"` and `schema_version: 3`.
They are separate from the demo benchmark's schema-v2 reports. The default output
is `reports/run.json`; supply a distinct `--output` to retain each run. Reports
are replaced atomically. The destination is checked before traffic, and it cannot
be the suite or its cases file.

Completed failures and Ctrl-C during traffic save partial evidence with an explicit
status. An initial `running` report is written before traffic. Process termination
with SIGKILL, machine failure, or loss of write access can prevent final evidence
from being saved; a `running` report is incomplete, never a pass. Per-request
samples are held in memory until finalization, not streamed to disk.

Reports omit request/response bodies, expected values, request headers, URL paths,
and queries. They retain the target origin, case names, a SHA-256 fingerprint of
the cases, workload settings, and any explicitly selected backend identity.
Choose those identifiers with report sharing in mind. Error diagnostics include
categories and failing check indices, never raw exception strings or response
values. Keep generated reports out of Git; CI artifacts or a curated, reviewed
experiment are the intended sharing mechanisms.

To bound retained evidence, `max_samples` defaults to 100,000 measured attempts.
Count-based workloads larger than this are rejected before traffic. A duration
run that reaches this cap before its deadline ends as an error with its partial
report. Increase the cap explicitly (maximum 1,000,000) for longer experiments.
Other limits are 1,000 concurrent requests, one hour of dispatch, 10,000 warmup
requests, a 300-second timeout, 10,000 cases, a 1 MiB suite, and a 16 MiB cases file.
Response bodies are buffered; streaming responses and very large outputs are
outside this first adapter's intended use.

[Reusable fault and rollout scenarios](scenarios.md) now coordinate these same
workloads with Toxiproxy or Kubernetes, including restoration and recovery checks.
[Repeated-run comparisons](comparisons.md) are also available. HTML reporting is
the [next milestone](proposals/inference-reliability.md#delivery-order). The reference
demo's four-scenario benchmark remains available.
