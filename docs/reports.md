# Shareable HTML reports

Render existing JSON evidence into a single HTML file:

```sh
uv run bront report reports/run.json --output reports/review.html
```

Open the file in a browser, or share it as a CI artifact. It works offline: all
styling, SVG charts, and downloadable JSON are embedded. No JavaScript, fonts,
external images, or service connections are required. Native expandable sections
keep detailed evidence accessible without making every table visible at once.

The default output is `reports/review.html`. Choose separate filenames to retain
multiple reports; successful rendering replaces an existing output atomically.
Inputs and referenced series children cannot be overwritten by the HTML output.

## Supported evidence

| Input | HTML contents |
| --- | --- |
| `bront.run`, schema 3 | Recorded verdict, assertions, traffic windows, failures, backend attribution, scenario timeline, recovery, configuration, provenance, and run JSON. |
| `bront.series`, schema 1 | Manifest status and an expandable view for each retained run, with manifest and child JSON downloads. |
| `bront.comparison`, schema 1 | Recorded verdict, evidence issues, compatibility differences, per-run distributions, regression checks, policy, context, and comparison JSON. |

```sh
# Run and render an external endpoint experiment.
uv run bront run examples/http-json/suite.toml --output reports/run.json
uv run bront report reports/run.json --output reports/run.html

# Render a repeated-run manifest, retaining each run's own assertions and samples.
uv run bront report reports/baseline.json --output reports/baseline.html

# Compare using an explicit policy, then render that recorded decision.
uv run bront compare reports/baseline.json reports/candidate.json \
  --policy limits.toml --output reports/comparison.json
uv run bront report reports/comparison.json --output reports/comparison.html
```

A comparison artifact contains metrics and provenance, not source request samples.
Its HTML shows the stored decision; it does not recompute that decision or retrieve
source files. Render source runs or manifests separately for detailed timelines.
Use `bront compare` to recalculate a release decision from trusted source evidence.
There is no separate `report --baseline` comparison workflow.

Old external-run schemas and the gateway demo's benchmark-v2 schema are not
converted. Their existing JSON remains available; collect schema-3 runs with
`bront run` for these HTML views.

## Reading a run

The headline counts and latency describe measured traffic. Warmup remains separate
in the experiment context and the downloaded JSON. A valid response satisfies its
configured status and output checks; valid-response availability is not simply an
HTTP 2xx count. Latency percentiles include failed attempts.

The chart and window table use at most 60 equal windows over measured elapsed time,
including the in-flight drain. Runs shorter than 60 seconds use approximately
one-second windows. A sample belongs to the window in which its request started;
its full response latency counts there even when it finishes later. Window
percentiles use the exact nearest-rank method, without dropping or sampling
attempts. Empty windows show no observation rather than zero latency or 100%
availability. Table boundaries are rounded to milliseconds for display; assignment
uses the full recorded precision.

Red latency bars identify windows containing failures. Numbered vertical markers
show scenario event starts on the measurement clock. The event table includes
scheduled, started, and finished times, status, and errors; preflight may have a
negative timestamp. Events on a preflight-only clock are not overlaid on measured
traffic. Alignment helps investigate an event and a failure; it does not establish
causation.

Service recovery and preferred-backend recovery remain distinct. Their clock starts
at the restore acknowledgement and ends at the first qualifying response completion.
Only requests dispatched after the acknowledgement qualify. Unknown backend
identity and unobserved recovery are explicit; success is not used to invent a
recovery measurement. Rollout-only scenarios do not imply reversible-fault recovery.

The failure table shows the first 100 measured failures in request order, including
case identifier, category, reason, and validation check index. Aggregate counts and
JSON include all retained attempts. Backend counts include all measured attempts,
not just valid responses. Metadata and warmup summaries are expandable.

## Evidence handling and limits

Run input is checked for sample/summary consistency using the comparison reader.
Series input additionally checks reference paths, digests, run identities, and
child consistency. Missing or inconsistent children fail rendering rather than
silently disappearing. Cancelled and incomplete series can render their retained
children; unfinished runs remain visibly incomplete. A fresh `running` snapshot
with no samples shows no evaluated assertions.

Comparison input receives schema validation. Its verdict is labelled **recorded**:
the renderer is a presentation tool, not an independent attestation. An author who
fabricates a consistent artifact cannot be detected from that artifact alone.

Rendering exits **0 when HTML was written**, including for failed or inconclusive
experiments. It exits **2** for invalid/unsupported evidence or output errors.
Keep `run` and `compare` exit codes as the CI release gates; rendering must not
replace those decisions. Existing output is retained on input-validation failure.

The reader limits input to 128 MiB per file and 256 MiB across a series, with at
most 100 child runs. Runner samples are already bounded by `workload.max_samples`
(default 100,000, maximum 1,000,000); exceeding that cap makes execution incomplete.
Rendering retains evidence in memory. Base64 JSON downloads enlarge HTML relative
to JSON; choose smaller experiments when artifact size or memory is constrained.

A run view embeds its complete structured JSON; a series embeds the manifest and
all retained children. To reconstruct a series from browser downloads, place each
run JSON at the **manifest child path** printed under its view. This preserves the
manifest digests. Downloading the original CI artifact directory is more convenient
when there are many children. Keep the directory structure intact.

Text from artifacts is HTML-escaped and the page disables scripts and network
assets with a Content Security Policy. The normal runner omits credentials and raw
request/response payloads. HTML does not add credentials, but it preserves supplied
metadata, case identifiers, failure reasons, and source evidence in downloads.
Review caller-declared labels before sharing; this is not a general secret scrubber.

## CI artifacts

The external endpoint smoke check renders passing, failing, interrupted, repeated,
inconclusive, and incompatible evidence:

```sh
uv run python scripts/smoke_external.py
```

The `external-endpoint-reports-py<version>` artifact contains both HTML and JSON, including
series children. Proxy scenario CI also renders reports and uploads the entire
`scenario-reports` directory, including on failure when evidence exists. Download
and open HTML locally; GitHub's artifact listing is not a hosted report viewer.

Generated reports stay under the ignored `reports/` directory. Keep them out of
Git unless publishing a deliberately curated experiment. JUnit export remains a
future option; the implemented formats are JSON evidence and offline HTML.
