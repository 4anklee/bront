# Bront

Repeatable reliability tests and release evidence for inference deployments.
Bront checks an existing HTTP JSON endpoint against representative inputs,
correctness assertions, and latency/error limits. Add fault or rollout scenarios,
compare repeated baseline and candidate runs, and share an offline HTML report.
Your service does not need to adopt Bront's reference gateway.

## Quickstart

Requires Python 3.12 or newer; tested on CPython 3.12, 3.13, and 3.14. From this checkout:

```sh
uv sync --frozen
uv run python examples/http-json/server.py
```

In another terminal:

```sh
uv run bront run examples/http-json/suite.toml --output reports/run.json
uv run bront report reports/run.json --output reports/review.html
```

Open `reports/review.html` in a browser. The standalone example requires no Docker,
Kubernetes, Triton, or backend-specific status API.

For your own service, configure the URL, JSONL input cases, expected outputs,
workload, and assertions in a TOML suite. Read the
[external endpoint guide](docs/external-endpoints.md) for the supported contract.
Credentials can reference environment variables; raw payloads and credential values
are excluded from normal reports.

## Release workflow

```sh
uv run bront run baseline.toml --repeat 3 --output reports/baseline.json
uv run bront run candidate.toml --repeat 3 --output reports/candidate.json
uv run bront compare reports/baseline.json reports/candidate.json \
  --policy limits.toml --output reports/comparison.json
uv run bront report reports/comparison.json --output reports/comparison.html
```

Record deployment metadata and explicitly allow the intended change in the
comparison policy. Bront checks experiment compatibility before evaluating
regressions. Missing context, incomplete execution, or insufficient repeat evidence
cannot produce a passing comparison. Observed variation is not a statistical
significance test.

| Command | Purpose |
| --- | --- |
| `bront run` | Execute a suite, retain samples, and evaluate release assertions. |
| `bront compare` | Compare compatible repeated experiments against an explicit policy. |
| `bront report` | Render recorded evidence as self-contained HTML with JSON downloads. |

`run` and `compare` return 0 for passing evidence, 1 for failed checks, and 2 for
execution/configuration problems or inconclusive/incompatible comparisons.
Interrupted runs use exit 130 and retain partial evidence. `report` returns 0 when
rendering succeeds; it does not replace a release gate.

## Guides

- [External endpoints and response validation](docs/external-endpoints.md)
- [Proxy faults and Kubernetes scenarios](docs/scenarios.md)
- [Repeated runs and release comparisons](docs/comparisons.md)
- [Offline HTML reports](docs/reports.md)
- [Architecture](docs/architecture.md)
- [Publishing Bront to PyPI](docs/publishing.md)
- [Roadmap and remaining validation](docs/proposals/inference-reliability.md)

## Optional reference deployment

The reference gateway, compatible mock fallback, Triton `double` model, fault
proxy, and monitoring stack exercise the same suite workflow. They are fixtures,
not required infrastructure for using Bront. Gateway code lives in
`bront.reference`; its server dependencies are an optional `reference` extra.

```sh
# Build and start the mock reference stack.
docker compose up --build --wait
uv run bront run examples/gateway/suite.toml --output reports/gateway.json
uv run python scripts/smoke_compose.py
```

For controlled outage and delay scenarios:

```sh
docker compose -f compose.yaml -f compose.scenarios.yaml up --build --wait
uv run bront run examples/scenarios/proxy-outage.toml --output reports/outage.json
uv run bront run examples/scenarios/proxy-delay.toml --output reports/delay.json
uv run bront report reports/outage.json --output reports/outage.html
```

Run one fault experiment at a time against a disposable reference stack. Scenarios
record control events and restore owned faults on ordinary errors and interruption.
The [scenario guide](docs/scenarios.md) explains cleanup and recovery boundaries.

See the [Triton walkthrough](docs/triton.md) for the ONNX CPU integration,
[Kubernetes guide](docs/kubernetes.md) for two independent gateway replicas, and
[observability guide](docs/observability.md) for per-pod metrics. The `double` model
is an integration fixture; a trained-model example remains on the roadmap.

For a locally launched gateway, copy `.env.example` to `.env` and install the
reference dependencies with `uv sync --extra reference`. Entry points are
`bront.reference.gateway:app` and `bront.reference.mock_backend:app`.
Reference environment variables use `BRONT_`; metrics use `bront_`.
The suite runner uses explicit TOML and process environment variables, not `.env`.

## Development and packaging

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run ty check --python-version 3.12
uv run pytest
uv run python scripts/smoke_external.py
uv build
```

The distribution, import package, and CLI are all named `bront`. A core wheel
installation needs no FastAPI or Prometheus dependencies. Source-checkout dev
requirements include the reference dependencies to test both workflows.
No registry publication is required to install a locally built wheel.

The `.python-version` file selects Python 3.14 for default checkout development;
it is not the package's installation requirement. To work on another supported version:

```sh
uv sync --python 3.12 --frozen
uv run --python 3.12 pytest
```

CI tests the full suite and an isolated wheel installation on Python 3.12, 3.13,
and 3.14, including the run/compare/report smoke workflow. Ruff and type checking
target the 3.12 minimum to catch accidental use of newer syntax or APIs.
The reference Docker images continue using Python 3.14.

The Dockerfile provides a `cli` build target for the tool alone and a default
`reference` target for the fixture services:

```sh
docker build --target cli -t bront:cli .
docker run --rm bront:cli
```

CI runs lint, types, tests, the reference Compose smoke check, reusable proxy
scenarios, and the standalone run/compare/report workflow. It uploads JSON and
HTML artifacts, including evidence retained after failed experiments. Generated
reports belong under the ignored `reports/` directory rather than in Git.
