import json

import pytest
from typer.testing import CliRunner

from inference_guard.artifacts import write_artifact
from inference_guard.checks import evaluate_measurement
from inference_guard.cli import app
from inference_guard.comparison_models import ComparisonPolicy
from inference_guard.comparisons import compare_files
from inference_guard.provenance import digest
from inference_guard.results import Attempt
from inference_guard.runs import make_report
from inference_guard.suites import Assertions, SuiteError, load_suite


@pytest.fixture
def experiment(tmp_path):
    suite = tmp_path / "suite.toml"
    suite.write_text("""version = 1
[target]
url = "http://baseline.test/predict"
[workload]
cases = "cases.jsonl"
requests = 20
concurrency = 1
[metadata]
model_revision = "model-v1"
serving_revision = "server-v1"
architecture = "x86_64"
[metadata.resources]
cpu = "2"
memory = "2Gi"
[metadata.labels]
environment = "test"
""")
    (tmp_path / "cases.jsonl").write_text(
        '{"name":"case","body":{"features":[1,2]},"expect":{"json":[{"pointer":"/value","equals":3}]}}'
    )
    loaded = load_suite(suite)

    def build(latency=10.0, *, failures=0, failure_kind="transport", modify=None):
        report = make_report(loaded)
        report.started_at = "2026-01-01T00:00:00+00:00"
        report.finished_at = "2026-01-01T00:01:00+00:00"
        report.warmup.summarize()
        phase = report.measurement
        phase.samples = [
            Attempt(
                index=i,
                case="case",
                started_ms=i * latency,
                duration_ms=float(latency),
                status_code=200,
                error=failure_kind if i < failures else None,
            )
            for i in range(20)
        ]
        phase.elapsed_seconds = 20 * latency / 1000 + 0.001
        phase.summarize()
        if modify:
            modify(report)
        report.provenance.contract_sha256 = digest(report.provenance.contract)
        report.provenance.missing_metadata = report.provenance.deployment.missing()
        report.assertions = evaluate_measurement(
            phase, Assertions.model_validate(report.provenance.contract["assertions"])
        )
        report.status = "passed" if all(a.passed for a in report.assertions) else "failed"
        return report

    return build


@pytest.fixture
def paths(tmp_path):
    def save(reports, side):
        result = []
        for index, report in enumerate(reports):
            path = tmp_path / f"{side}-{index}.json"
            write_artifact(report, path)
            result.append(path)
        return result

    return save


def policy(**changes):
    return ComparisonPolicy.model_validate({"version": 1, "minimum_requests": 20, **changes})


@pytest.mark.parametrize(("candidate", "status"), [(11.0, "passed"), (13.0, "failed")])
def test_repeated_latency_regression_verdicts(experiment, paths, candidate, status):
    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths([experiment(candidate) for _ in range(3)], "candidate")
    result = compare_files(left, right, policy(max_p95_increase_percent=20))
    assert result.status == status
    assert result.checks[0].status == status
    assert result.metrics["p95_ms"].candidate.values == [candidate] * 3
    assert result.metrics["p95_ms"].baseline.median == 10


def test_variation_crossing_limit_is_not_a_small_regression_claim(experiment, paths):
    left = paths([experiment(n) for n in (9.0, 10.0, 11.0)], "baseline")
    right = paths([experiment(n) for n in (10.0, 11.0, 12.0)], "candidate")
    result = compare_files(left, right, policy(max_p95_increase_percent=20))
    assert result.status == "inconclusive"
    assert result.checks[0].reason == "variation_crosses_limit"
    lower, upper = result.checks[0].observed_lower, result.checks[0].observed_upper
    assert lower is not None and upper is not None
    assert lower < 20 < upper


def test_throughput_drop_direction(experiment, paths):
    left = paths([experiment(10.0) for _ in range(3)], "baseline")
    right = paths([experiment(20.0) for _ in range(3)], "candidate")
    result = compare_files(left, right, policy(max_throughput_drop_percent=10))
    assert result.status == "failed"
    lower = result.checks[0].observed_lower
    assert lower is not None and lower > 40


def test_single_runs_show_descriptive_metrics_without_release_gates(experiment, paths):
    result = compare_files(
        paths([experiment()], "baseline"),
        paths([experiment(20.0)], "candidate"),
        policy(max_p95_increase_percent=10),
    )
    assert result.status == "inconclusive"
    assert result.metrics["p95_ms"].candidate.median == 20
    assert result.checks == []


def test_candidate_release_failure_is_not_hidden_by_fast_latency(experiment, paths):
    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths(
        [experiment(1.0, failures=1, failure_kind="validation") for _ in range(3)], "candidate"
    )
    result = compare_files(left, right, policy(max_p95_increase_percent=20))
    assert result.status == "failed"
    assert result.checks[0].status == "passed"
    assert result.run_summaries[-1]["errors"] == {"validation": 1}


def test_error_rate_increase_uses_fraction_not_relative_zero_baseline(experiment, paths):
    def budget(report):
        report.provenance.contract["assertions"]["max_failed_requests"] = 2

    left = paths([experiment(modify=budget) for _ in range(3)], "baseline")
    right = paths([experiment(failures=1, modify=budget) for _ in range(3)], "candidate")
    result = compare_files(left, right, policy(max_error_rate_increase=0.01))
    assert result.status == "failed"
    assert result.checks[0].observed_lower == 0.05


def test_zero_baseline_latency_does_not_create_infinity(experiment, paths):
    left = paths([experiment(0.0) for _ in range(3)], "baseline")
    right = paths([experiment(10.0) for _ in range(3)], "candidate")
    result = compare_files(left, right, policy(max_p95_increase_percent=20))
    assert result.status == "inconclusive"
    assert result.checks[0].reason == "relative_change_undefined"
    assert "Infinity" not in result.model_dump_json()


@pytest.mark.parametrize(
    "field", ["concurrency", "timeout", "assertions", "method", "route", "cases", "runner"]
)
def test_unaccounted_material_changes_are_incompatible(experiment, paths, field):
    def alter(report):
        contract = report.provenance.contract
        if field == "concurrency":
            report.workload["concurrency"] = 2
            contract["workload"]["concurrency"] = 2
        elif field == "timeout":
            # Retain a complete run with a different per-request deadline.
            report.workload["request_timeout_seconds"] = 10.0
            contract["workload"]["request_timeout_seconds"] = 10.0
        elif field == "assertions":
            contract["assertions"]["max_p95_ms"] = 100.0
        elif field == "method":
            contract["method"] = report.target["method"] = "PUT"
        elif field == "route":
            contract["route_sha256"] = "b" * 64
        elif field == "cases":
            report.cases_sha256 = contract["cases_sha256"] = "b" * 64
        else:
            report.provenance.runner["python"] = "3.15.0"

    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths([experiment(modify=alter) for _ in range(3)], "candidate")
    result = compare_files(left, right, policy())
    assert result.status == "incompatible", result.issues
    assert result.differences and not result.differences[0].allowed


def test_endpoint_origin_can_change_but_revision_needs_explicit_intent(experiment, paths):
    def alter(report):
        report.target["origin"] = "http://candidate.test/"
        report.provenance.deployment.serving_revision = "server-v2"

    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths([experiment(modify=alter) for _ in range(3)], "candidate")
    assert compare_files(left, right, policy()).status == "incompatible"
    result = compare_files(left, right, policy(allowed_changes=["deployment.serving_revision"]))
    assert result.status == "passed"
    assert [d.field for d in result.differences] == ["deployment.serving_revision"]
    assert result.differences[0].allowed


def test_cohort_mixing_cannot_be_excused_by_allowed_candidate_change(experiment, paths):
    candidates = [experiment() for _ in range(3)]
    candidates[-1].provenance.deployment.serving_revision = "server-v2"
    result = compare_files(
        paths([experiment() for _ in range(3)], "baseline"),
        paths(candidates, "candidate"),
        policy(allowed_changes=["deployment.serving_revision"]),
    )
    assert result.status == "incompatible"
    assert any(d.field.startswith("candidate[2]") and not d.allowed for d in result.differences)


@pytest.mark.parametrize(
    "problem",
    ["single", "samples", "duration", "metadata", "label", "incomplete", "baseline_failed"],
)
def test_insufficient_evidence_cannot_pass(experiment, paths, problem):
    left = [experiment() for _ in range(1 if problem == "single" else 3)]
    right = [experiment() for _ in range(1 if problem == "single" else 3)]
    settings = policy()
    if problem == "samples":
        settings.minimum_requests = 100
    elif problem == "duration":
        settings.minimum_duration_seconds = 10
    elif problem == "metadata":
        for run in left + right:
            run.provenance.deployment.resources = {}
            run.provenance.missing_metadata = ["resources"]
    elif problem == "label":
        settings.required_labels = ["hardware"]
    elif problem == "incomplete":
        right[-1].status = "cancelled"
    elif problem == "baseline_failed":
        left[0] = experiment(failures=1)
    result = compare_files(paths(left, "baseline"), paths(right, "candidate"), settings)
    assert result.status == "inconclusive"
    assert result.issues


def test_duplicate_report_copies_are_not_repeated_experiments(experiment, paths):
    one = experiment()
    result = compare_files(
        paths([one] * 3, "baseline"), paths([experiment() for _ in range(3)], "candidate"), policy()
    )
    assert result.status == "inconclusive"
    assert "duplicate_run_identity" in result.issues


@pytest.mark.parametrize(
    "tamper",
    ["summary", "fingerprint", "verdict", "negative_latency", "truncated", "contract", "legacy"],
)
def test_malformed_or_inconsistent_reports_never_pass(experiment, paths, tamper):
    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths([experiment() for _ in range(3)], "candidate")
    raw = json.loads(right[0].read_text())
    if tamper == "summary":
        raw["measurement"]["latency_ms"]["p95"] = 0
    elif tamper == "fingerprint":
        raw["provenance"]["contract_sha256"] = "0" * 64
    elif tamper == "verdict":
        raw["assertions"][0]["passed"] = False
    elif tamper == "negative_latency":
        raw["measurement"]["samples"][0]["duration_ms"] = -1
    elif tamper == "truncated":
        raw["measurement"]["samples"].pop()
    elif tamper == "contract":
        raw["provenance"]["contract"].pop("route_sha256")
        raw["provenance"]["contract_sha256"] = digest(raw["provenance"]["contract"])
    else:
        raw["schema_version"] = 2
    right[0].write_text(json.dumps(raw))
    result = compare_files(left, right, policy())
    assert result.status == "inconclusive"
    assert result.issues


def test_output_cannot_overwrite_any_input(experiment, paths):
    left, right = paths([experiment()], "baseline"), paths([experiment()], "candidate")
    original = left[0].read_bytes()
    with pytest.raises(SuiteError):
        compare_files(left, right, policy(), output=left[0])
    assert left[0].read_bytes() == original


@pytest.mark.parametrize(("candidate_latency", "exit_code"), [(10.0, 0), (20.0, 1)])
def test_compare_cli_and_artifact(experiment, paths, tmp_path, candidate_latency, exit_code):
    left = paths([experiment() for _ in range(3)], "baseline")
    right = paths([experiment(candidate_latency) for _ in range(3)], "candidate")
    limits = tmp_path / "limits.toml"
    limits.write_text("version = 1\nminimum_requests = 20\nmax_p95_increase_percent = 20\n")
    output = tmp_path / "comparison.json"
    args = [
        "compare",
        str(left[0]),
        str(right[0]),
        "--policy",
        str(limits),
        "--output",
        str(output),
        "--json",
    ]
    for path in left[1:]:
        args += ["--baseline-run", str(path)]
    for path in right[1:]:
        args += ["--candidate-run", str(path)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.output) == json.loads(output.read_text())


def test_policy_cannot_allow_workload_or_correctness_changes():
    for key in ("contract.workload", "contract.assertions", "contract.cases_sha256", "runner"):
        with pytest.raises(ValueError):
            policy(allowed_changes=[key])
