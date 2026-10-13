"""Self-contained, script-free HTML views of recorded Bront evidence."""

import base64
import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from pathlib import Path

from bront.comparison_models import ComparisonReport
from bront.evidence import ReadBudget, load_evidence, validate_run
from bront.measurement import percentile
from bront.report_styles import CSS
from bront.results import Attempt, RunReport
from bront.scenario_models import ScenarioEvidence
from bront.series import SeriesReport
from bront.suites import SuiteError


def text(value: object) -> str:
    return escape(str(value), quote=True)


def number(value: float | None) -> str:
    return "Not observed" if value is None else f"{value:,.6g}"


def table(headers: list[str], rows: Iterable[Iterable[object]], caption: str = "") -> str:
    heading = "".join(f'<th scope="col">{text(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{text(v)}</td>" for v in row) + "</tr>" for row in rows)
    return (
        '<div class="scroll"><table>'
        + (f"<caption>{text(caption)}</caption>" if caption else "")
        + f"<thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def details(title: str, value: object) -> str:
    return (
        f"<details><summary>{text(title)}</summary>"
        f"<pre>{text(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>"
    )


def badge(status: str) -> str:
    known = {
        "passed",
        "failed",
        "error",
        "inconclusive",
        "incompatible",
        "cancelled",
        "running",
        "incomplete",
    }
    style = status if status in known else "incomplete"
    return f'<span class="badge {style}">{text(status)}</span>'


def download(value: object, filename: str, label: str) -> str:
    encoded = base64.b64encode(
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).decode("ascii")
    return (
        f'<a download="{text(filename)}" href="data:application/json;base64,{encoded}">'
        f"{text(label)}</a>"
    )


@dataclass
class Window:
    start: float
    end: float
    samples: list[Attempt]

    @property
    def failed(self) -> int:
        return sum(sample.error is not None for sample in self.samples)

    def latency(self, percent: float) -> float | None:
        return percentile([s.duration_ms for s in self.samples], percent) if self.samples else None


def windows(run: RunReport) -> list[Window]:
    """At most 60 equal windows, assigned by request start; never downsample attempts."""
    elapsed = run.measurement.elapsed_seconds
    if elapsed <= 0:
        return []
    count = min(60, max(1, math.ceil(elapsed)))
    width = elapsed / count
    result = [Window(i * width, (i + 1) * width, []) for i in range(count)]
    for sample in run.measurement.samples:
        result[min(count - 1, int(sample.started_ms / 1000 / width))].samples.append(sample)
    return result


def latency_chart(bins: list[Window], scenario: ScenarioEvidence | None = None) -> str:
    if not bins or not any(w.samples for w in bins):
        return '<p class="note">No measured latency samples.</p>'
    peak = max(w.latency(95) or 0 for w in bins)
    marks = []
    for i, window in enumerate(bins):
        value = window.latency(95)
        if value is None:
            continue
        x = 55 + (i + 0.5) * 760 / len(bins)
        height = value / peak * 125 if peak else 0
        color = "#b53642" if window.failed else "#386f9f"
        label = (
            f"{window.start:.3f}–{window.end:.3f}s: p95 {number(value)} ms; "
            f"{window.failed}/{len(window.samples)} failed"
        )
        marks.append(
            f'<rect x="{x - 760 / len(bins) * 0.35:.2f}" y="{155 - height:.2f}" '
            f'width="{760 / len(bins) * 0.7:.2f}" height="{max(height, 2):.2f}" '
            f'fill="{color}"><title>{text(label)}</title></rect>'
        )
    if scenario is not None and scenario.clock_origin == "measurement":
        for index, event in enumerate(scenario.events, 1):
            if not 0 <= event.started_ms <= bins[-1].end * 1000:
                continue
            x = 55 + event.started_ms / (bins[-1].end * 1000) * 760
            label = f"Event {index}: {event.action}, started {event.started_ms:.3f} ms"
            marks.append(
                f'<path d="M{x:.2f} 25V157" stroke="#8d641d" stroke-dasharray="3 3">'
                f"<title>{text(label)}</title></path>"
                f'<text x="{x:.2f}" y="{35 + (index % 2) * 14}">{index}</text>'
            )
    return (
        '<div class="scroll"><svg viewBox="0 0 850 200" role="img" '
        'aria-label="Latency by request start window">'
        f"<title>Per-window p95 latency; red bars contain failed attempts</title>"
        '<path d="M55 25V157H815" fill="none" stroke="#bbc8d5"/>'
        f'<text x="55" y="17">{text(number(peak))} ms maximum</text>'
        '<text x="55" y="184">0 seconds</text>'
        f'<text x="815" y="184" text-anchor="end">{bins[-1].end:.3f} seconds</text>'
        + "".join(marks)
        + "</svg></div>"
    )


def run_view(run: RunReport) -> str:
    phase = run.measurement
    bins = windows(run)
    stats = (
        (f"{phase.successful:,} / {phase.requests:,}", "Valid responses"),
        (f"{phase.failed:,}", "Failed attempts"),
        (number(phase.latency_ms.get("p95")), "p95 latency · ms"),
        (number(phase.requests_per_second), "Attempted requests/s"),
    )
    parts = [
        badge(run.status),
        f'<p class="subtitle">Run <code>{text(run.provenance.run_id)}</code><br>'
        f"{text(run.started_at)} → {text(run.finished_at or 'Still running')}</p>",
        '<div class="stats">'
        + "".join(
            f'<div class="stat"><strong>{text(value)}</strong><span>{label}</span></div>'
            for value, label in stats
        )
        + "</div>",
    ]
    if run.reason or run.status not in ("passed", "failed"):
        parts.append(
            f'<p class="warning">Incomplete execution: {text(run.reason or run.status)}. '
            "Measurements describe retained attempts only; this is not a passing release.</p>"
        )
    parts += [
        "<h2>Release assertions</h2>",
        table(
            ["Check", "Result", "Observed", "Maximum"],
            (
                (a.name, "passed" if a.passed else "failed", number(a.actual), number(a.limit))
                for a in run.assertions
            ),
        )
        if run.assertions
        else "<p>No release assertions were evaluated.</p>",
        "<h2>Measured traffic</h2>",
        '<p class="note">Closed-loop traffic. Latency includes failed attempts; warmup is '
        "excluded. Empty windows have no observed availability or latency. "
        "Red bars contain failed attempts; numbered markers show scenario event starts. "
        "Timing alignment does not establish causation.</p>",
        latency_chart(bins, run.scenario),
        "<details><summary>Per-window availability and latency</summary>",
        table(
            ["Start–end (s)", "Attempts", "Valid (%)", "Failed", "p95 (ms)", "p99 (ms)"],
            (
                (
                    f"{w.start:.3f}–{w.end:.3f}",
                    len(w.samples),
                    f"{100 * (1 - w.failed / len(w.samples)):.2f}" if w.samples else "Not observed",
                    w.failed,
                    number(w.latency(95)),
                    number(w.latency(99)),
                )
                for w in bins
            ),
            "At most 60 equal windows over elapsed measurement including drain; grouped by request "
            "start. Exact nearest-rank percentiles within each window.",
        ),
        "</details>",
        "<h2>Failures</h2>",
        table(["Category", "Count"], sorted(phase.errors.items()))
        if phase.errors
        else "<p>No measured failures.</p>",
    ]
    failures = [s for s in phase.samples if s.error is not None]
    if failures:
        parts += [
            table(
                ["Request", "Case", "Start (ms)", "Category", "Reason", "Check index"],
                (
                    (
                        s.index,
                        s.case,
                        number(s.started_ms),
                        s.error,
                        s.reason or s.error_type or "—",
                        s.check_index if s.check_index is not None else "—",
                    )
                    for s in failures[:100]
                ),
                "First 100 failed attempts in request order; "
                "full samples remain in the JSON evidence.",
            ),
        ]
    backends = Counter(s.backend if s.backend is not None else "(unknown)" for s in phase.samples)
    parts += ["<h2>Backend attribution</h2>", table(["Identity", "Attempts"], backends.items())]
    scenario = run.scenario
    if scenario is not None:
        parts += [
            "<h2>Scenario timeline</h2>",
            f"<p>{text(scenario.kind)} · cleanup: <strong>{text(scenario.cleanup)}</strong> · "
            f"clock origin: {text(scenario.clock_origin)} · completed: {scenario.completed}</p>",
            table(
                ["Event", "Scheduled (ms)", "Started (ms)", "Finished (ms)", "Status", "Error"],
                (
                    (
                        f"{index}. {e.action}",
                        number(e.scheduled_ms),
                        number(e.started_ms),
                        number(e.finished_ms),
                        e.status,
                        e.error or "—",
                    )
                    for index, e in enumerate(scenario.events, 1)
                ),
                "Times use the recorded clock origin; "
                "preflight may precede measurement at negative time.",
            ),
            details(
                "Scenario configuration and observations",
                {
                    "configuration": scenario.configuration,
                    "observations": scenario.observations,
                },
            ),
        ]
        recovery = scenario.recovery
        parts.append("<h2>Recovery</h2>")
        if recovery is None:
            parts.append(
                "<p>No recovery evidence recorded; recovery is not inferred from success.</p>"
            )
        else:
            parts += [
                f'<p class="note">Clock starts at restore acknowledgement '
                f"({text(number(recovery.from_ms))} ms). Only valid requests started after that "
                "acknowledgement qualify; elapsed time ends at response completion.</p>",
                table(
                    ["Observation", "Milliseconds after acknowledgement"],
                    [
                        ("First valid response", number(recovery.first_valid_response_ms)),
                        (
                            "First preferred backend "
                            f"({recovery.preferred_backend or 'not configured'})",
                            number(recovery.first_preferred_backend_ms)
                            if recovery.backend_identity_available
                            else "Identity unavailable",
                        ),
                    ],
                ),
            ]
    parts += [
        "<h2>Experiment context</h2>",
        '<p class="note">Deployment metadata is caller-declared, not attested. '
        "Missing metadata: " + text(", ".join(run.provenance.missing_metadata) or "none") + ".</p>",
        details(
            "Target, workload, and warmup",
            {
                "target": run.target,
                "workload": run.workload,
                "warmup": run.warmup.model_dump(mode="json", exclude={"samples"}),
            },
        ),
        details("Provenance and comparison contract", run.provenance.model_dump(mode="json")),
        '<div class="downloads">'
        + download(
            run.model_dump(mode="json"), f"{run.provenance.run_id}.json", "Download run JSON"
        )
        + "</div>",
    ]
    return "".join(parts)


def comparison_view(report: ComparisonReport) -> str:
    parts = [
        badge(report.status),
        f'<p class="subtitle">Recorded comparison · {text(report.created_at)} · '
        f"{len(report.baseline_run_ids)} baseline / "
        f"{len(report.candidate_run_ids)} candidate runs</p>",
        '<p class="note">This view preserves the recorded comparison verdict; it does not '
        "re-run checks or retrieve source runs. Observed ranges are descriptive, "
        "not confidence intervals or evidence of statistical significance.</p>",
        "<h2>Evidence issues</h2>",
        table(["Issue"], ((issue,) for issue in report.issues))
        if report.issues
        else "<p>No evidence issues recorded.</p>",
        "<h2>Compatibility</h2>",
        table(
            ["Field", "Baseline", "Candidate", "Allowed"],
            (
                (
                    d.field,
                    json.dumps(d.baseline, ensure_ascii=False),
                    json.dumps(d.candidate, ensure_ascii=False),
                    d.allowed,
                )
                for d in report.differences
            ),
        )
        if report.differences
        else "<p>No differences recorded.</p>",
        "<h2>Per-run metric distributions</h2>",
        table(
            [
                "Metric",
                "Units",
                "Baseline min / median / max",
                "Candidate min / median / max",
                "Median change (%)",
            ],
            (
                (
                    name,
                    m.unit,
                    " / ".join(
                        number(n)
                        for n in (m.baseline.minimum, m.baseline.median, m.baseline.maximum)
                    ),
                    " / ".join(
                        number(n)
                        for n in (m.candidate.minimum, m.candidate.median, m.candidate.maximum)
                    ),
                    number(m.median_change_percent),
                )
                for name, m in report.metrics.items()
            ),
            "Each value summarizes whole runs; samples from different runs are never pooled.",
        ),
        "<h2>Regression checks</h2>",
        table(
            ["Metric", "Result", "Limit", "Observed change range", "Reason"],
            (
                (
                    c.name,
                    c.status,
                    number(c.limit),
                    f"{number(c.observed_lower)} to {number(c.observed_upper)}",
                    c.reason or "—",
                )
                for c in report.checks
            ),
            "Latency increases and throughput drops use percentages; "
            "error-rate changes use absolute "
            "fractions. Unconfigured checks are descriptive only.",
        )
        if report.checks
        else "<p>No relative checks evaluated. Consult the policy and evidence issues.</p>",
        "<h2>Individual runs</h2>",
        table(
            ["Group", "Run ID", "Status", "Attempts", "Failed", "Failed assertions"],
            (
                (
                    s.get("side"),
                    s.get("run_id"),
                    s.get("status"),
                    s.get("requests"),
                    s.get("failed"),
                    json.dumps(s.get("failed_assertions")),
                )
                for s in report.run_summaries
            ),
        ),
        details("Release policy", report.policy.model_dump(mode="json")),
        details("Baseline context", report.baseline_context),
        details("Candidate context", report.candidate_context),
        details(
            "Per-run metric values and error counts",
            {
                "metrics": {
                    key: value.model_dump(mode="json") for key, value in report.metrics.items()
                },
                "runs": report.run_summaries,
            },
        ),
        '<div class="downloads">'
        + download(report.model_dump(mode="json"), "comparison.json", "Download comparison JSON")
        + "</div>",
    ]
    return "".join(parts)


def document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'\">"
        f"<title>{text(title)} · Bront</title><style>{CSS}</style></head><body><main>"
        '<header><div class="brand">BRONT / EXPERIMENT REVIEW</div>'
        f'<h1>{text(title)}</h1><p class="subtitle">Inference reliability evidence</p></header>'
        + body
        + "<footer>Generated from recorded evidence. No scripts, external assets, or network "
        "requests. JSON downloads preserve the supplied evidence; review metadata before sharing."
        "</footer></main></body></html>\n"
    )


def render_file(source: Path, output: Path) -> str:
    """Load bounded evidence, render offline, and protect all source artifacts."""
    budget = ReadBudget()
    raw = budget.read(source)
    kind = raw.get("report_type")
    if kind == "bront.comparison":
        report = ComparisonReport.model_validate(raw)
        title = "Release comparison"
        body = "<section>" + comparison_view(report) + "</section>"
    elif kind == "bront.run":
        run = validate_run(raw)
        title = "Inference run"
        body = "<section>" + run_view(run) + "</section>"
    elif kind == "bront.series":
        series = SeriesReport.model_validate(raw)
        # Reuse comparison loading to enforce digest, path, identity, and run consistency checks.
        budget = ReadBudget()
        evidence = load_evidence([source], "series", budget)
        if any(issue != "series[0]:series_incomplete" for issue in evidence.issues):
            raise SuiteError("Series evidence is missing or inconsistent; check its child reports")
        title = "Repeated inference runs"
        body = "<section>" + badge("incomplete" if evidence.issues else series.status)
        body += f"<p>{len(evidence.runs)} / {series.requested_runs} runs recorded.</p>"
        if series.reason or evidence.issues:
            body += f'<p class="warning">{text(series.reason or "Series incomplete")}</p>'
        body += "<p>Each run retains its own samples and assertions; no pooled release verdict.</p>"
        body += download(raw, "series.json", "Download series manifest") + "</section>"
        # Preserve manifest-relative child names in downloads for a reconstructable bundle.
        for index, (run, reference) in enumerate(zip(evidence.runs, series.runs, strict=True), 1):
            body += (
                f'<details class="run"><summary>Run {index} · {text(run.status)}</summary>'
                + run_view(run)
                + "<p>Manifest child path: <code>"
                + text(reference.path)
                + "</code>. Restore the downloaded run to this path to reconstruct the series.</p>"
                + "</details>"
            )
    else:
        raise SuiteError("Supported artifacts: Bront run schema 3, series 1, or comparison 1")
    if output.resolve() in budget.paths:
        raise SuiteError("HTML output must not overwrite an input artifact")
    return document(title, body)
