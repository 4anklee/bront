import base64
import json
from html.parser import HTMLParser

import pytest
from typer.testing import CliRunner

from bront.artifacts import write_artifact
from bront.checks import evaluate_measurement
from bront.cli import app
from bront.comparison_models import ComparisonPolicy, ComparisonReport
from bront.html_reports import render_file, windows
from bront.provenance import digest
from bront.results import Attempt
from bront.runs import make_report
from bront.series import RunReference, SeriesReport
from bront.suites import SuiteError, load_suite


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.downloads = []
        self.content = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            href = dict(attrs)["href"]
            assert href.startswith("data:application/json;base64,")
            self.downloads.append(json.loads(base64.b64decode(href.split(",", 1)[1])))

    def handle_data(self, data):
        self.content.append(data)


@pytest.fixture
def experiment(tmp_path):
    path = tmp_path / "suite.toml"
    path.write_text("""version = 1
[target]
url = "http://model/predict"
headers = { Authorization = "Bearer must-never-be-in-html" }
[workload]
cases = "cases.jsonl"
requests = 4
concurrency = 1
""")
    (tmp_path / "cases.jsonl").write_text(
        '{"name":"case","body":{"private":"payload-secret"},"expect":{"status":200}}'
    )
    loaded = load_suite(path)

    def build(*, failed=False, initial=False):
        run = make_report(loaded)
        if not initial:
            run.started_at = "2026-01-01T00:00:00+00:00"
            run.finished_at = "2026-01-01T00:00:05+00:00"
            run.warmup.summarize()
            run.measurement.elapsed_seconds = 4
            run.measurement.samples = [
                Attempt(
                    index=i,
                    case="case",
                    started_ms=start,
                    duration_ms=10.0 * (i + 1),
                    status_code=200,
                    error="validation" if failed and i == 0 else None,
                    reason="wrong_value" if failed and i == 0 else None,
                    check_index=0 if failed and i == 0 else None,
                )
                for i, start in enumerate([0, 1000, 1001, 3900])
            ]
            run.measurement.summarize()
            run.assertions = evaluate_measurement(run.measurement, loaded.suite.assertions)
            run.status = "failed" if failed else "passed"
        return run

    return build


def save(tmp_path, report, name="run.json"):
    source = tmp_path / name
    write_artifact(report, source)
    return source


def test_window_boundaries_and_percentiles_include_failures(experiment):
    bins = windows(experiment(failed=True))
    assert [len(w.samples) for w in bins] == [1, 2, 0, 1]
    assert bins[0].failed == 1
    assert bins[0].latency(95) == 10
    assert bins[1].latency(95) == 30
    assert bins[2].latency(95) is None
    run = experiment()
    run.measurement.elapsed_seconds = 1000
    assert len(windows(run)) == 60
    assert sum(len(w.samples) for w in windows(run)) == 4


@pytest.mark.parametrize("failed", [False, True])
def test_run_html_preserves_verdict_failures_and_embedded_evidence(tmp_path, experiment, failed):
    run = experiment(failed=failed)
    page = Page(render_file(save(tmp_path, run), tmp_path / "review.html"))
    words = " ".join(page.content)
    assert run.status in words
    assert "Valid responses" in words
    assert "Exact nearest-rank" in words
    assert ("wrong_value" in words) is failed
    assert page.downloads == [run.model_dump(mode="json")]
    assert "must-never-be-in-html" not in words
    assert "payload-secret" not in words


def test_untrusted_text_cannot_create_markup_scripts_or_network_requests(tmp_path, experiment):
    run = experiment(failed=True)
    attack = '</style><script>alert(1)</script><img src="https://evil.test" onerror="alert(2)">'
    run.provenance.deployment.labels["note"] = attack + " · 日本語"
    run.measurement.samples[0].reason = attack
    html = render_file(save(tmp_path, run), tmp_path / "review.html")
    page = Page(html)
    assert not {"script", "img", "iframe", "link", "object"} & {tag for tag, _ in page.tags}
    assert not any(key.startswith("on") for _, attrs in page.tags for key in attrs)
    assert any(attrs.get("http-equiv") == "Content-Security-Policy" for _, attrs in page.tags)
    assert attack in " ".join(page.content)
    assert page.downloads[0]["provenance"]["deployment"]["labels"]["note"] == attack + " · 日本語"


@pytest.mark.parametrize("status", ["running", "cancelled", "error"])
def test_incomplete_and_initial_runs_remain_visibly_incomplete(tmp_path, experiment, status):
    run = experiment(initial=status == "running")
    run.status = status
    html = render_file(save(tmp_path, run), tmp_path / "review.html")
    assert "Incomplete execution" in html
    assert "not a passing release" in html
    if status == "running":
        assert "No measured latency samples" in html
        assert "No release assertions were evaluated" in html


@pytest.mark.parametrize("status", ["passed", "failed", "inconclusive", "incompatible"])
def test_comparison_preserves_recorded_verdict_and_policy(tmp_path, status):
    comparison = ComparisonReport(
        policy=ComparisonPolicy(version=1), status=status, issues=["baseline:insufficient_repeats"]
    )
    page = Page(render_file(save(tmp_path, comparison), tmp_path / "review.html"))
    assert status in page.content
    assert "baseline:insufficient_repeats" in page.content
    assert "Recorded comparison" in " ".join(page.content)
    assert page.downloads == [comparison.model_dump(mode="json")]


def series(tmp_path, experiment):
    manifest = SeriesReport(requested_runs=2, status="passed", finished_at="2026-01-01T00:00:06Z")
    for index in range(2):
        run = experiment()
        path = f"children/{index}.json"
        save(tmp_path, run, path)
        manifest.runs.append(
            RunReference(
                path=path, run_id=run.provenance.run_id, sha256=digest(run.model_dump(mode="json"))
            )
        )
    return manifest


def test_series_embeds_children_and_manifest_with_restore_paths(tmp_path, experiment):
    manifest = series(tmp_path, experiment)
    page = Page(render_file(save(tmp_path, manifest), tmp_path / "review.html"))
    assert len(page.downloads) == 3
    assert page.downloads[0] == manifest.model_dump(mode="json")
    for reference, child in zip(manifest.runs, page.downloads[1:], strict=True):
        assert reference.path in page.content
        assert digest(child) == reference.sha256


def test_partial_series_renders_retained_children(tmp_path, experiment):
    manifest = series(tmp_path, experiment)
    manifest.status, manifest.reason = "cancelled", "interrupted"
    manifest.runs.pop()
    html = render_file(save(tmp_path, manifest), tmp_path / "review.html")
    assert "badge incomplete" in html
    assert "interrupted" in html
    assert len(Page(html).downloads) == 2


@pytest.mark.parametrize("problem", ["missing", "tampered", "escape"])
def test_series_refuses_broken_or_unsafe_child_references(tmp_path, experiment, problem):
    manifest = series(tmp_path, experiment)
    if problem == "missing":
        (tmp_path / manifest.runs[0].path).unlink()
    elif problem == "tampered":
        (tmp_path / manifest.runs[0].path).write_text("{}")
    else:
        manifest.runs[0].path = "../outside.json"
    with pytest.raises(SuiteError, match="missing or inconsistent"):
        render_file(save(tmp_path, manifest), tmp_path / "review.html")


def test_html_cannot_overwrite_source_or_series_child(tmp_path, experiment):
    manifest = series(tmp_path, experiment)
    source = save(tmp_path, manifest)
    for output in (source, tmp_path / manifest.runs[0].path):
        original = output.read_bytes()
        result = CliRunner().invoke(app, ["report", str(source), "--output", str(output)])
        assert result.exit_code == 2
        assert output.read_bytes() == original


@pytest.mark.parametrize(
    "raw",
    ["{}", '{"report_type":"bront.run","schema_version":2}', "{", '{"bad":NaN}', '{"x":1,"x":2}'],
)
def test_invalid_inputs_do_not_replace_existing_output(tmp_path, raw):
    source, output = tmp_path / "bad.json", tmp_path / "review.html"
    source.write_text(raw)
    output.write_text("previous report")
    result = CliRunner().invoke(app, ["report", str(source), "--output", str(output)])
    assert result.exit_code == 2
    assert output.read_text() == "previous report"


def test_inconsistent_run_summary_is_rejected(tmp_path, experiment):
    run = experiment()
    run.measurement.successful = 999
    result = CliRunner().invoke(app, ["report", str(save(tmp_path, run))])
    assert result.exit_code == 2


def test_render_exit_code_is_not_a_release_verdict(tmp_path, experiment):
    source = save(tmp_path, experiment(failed=True))
    output = tmp_path / "nested/review.html"
    result = CliRunner().invoke(app, ["report", str(source), "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.read_text().startswith("<!doctype html>")
    assert "badge failed" in output.read_text()
