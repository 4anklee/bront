"""Load external endpoint suites without making network calls or leaking input values."""

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import httpx
from pydantic import Field, HttpUrl, JsonValue, ValidationError, field_validator, model_validator

from inference_guard.provenance import DeploymentMetadata
from inference_guard.scenario_models import KubernetesRestart, ScenarioConfig
from inference_guard.validation import (
    JsonCheck,
    StrictModel,
    finite_json,
    parse_json,
    pointer_tokens,
)


class SuiteError(ValueError):
    pass


class HeaderEnv(StrictModel):
    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    prefix: str = ""


class BackendIdentity(StrictModel):
    pointer: str | None = None
    header: str | None = None

    @model_validator(mode="after")
    def select_source(self) -> Self:
        if (self.pointer is None) == (self.header is None):
            raise ValueError("Select exactly one backend pointer or header")
        if self.pointer is not None:
            pointer_tokens(self.pointer)
        if self.header is not None and not re.fullmatch(
            r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.header
        ):
            raise ValueError("Invalid backend header name")
        return self


class Target(StrictModel):
    adapter: Literal["http-json"] = "http-json"
    url: HttpUrl
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "POST"
    headers: dict[str, str | HeaderEnv] = Field(default_factory=dict)
    backend: BackendIdentity | None = None

    @field_validator("url")
    @classmethod
    def no_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None or value.fragment is not None:
            raise ValueError(
                "Use headers for credentials; URL userinfo and fragments are unsupported"
            )
        return value


class Workload(StrictModel):
    cases: str
    concurrency: int = Field(default=10, ge=1, le=1000)
    requests: int | None = Field(default=None, ge=1, le=1_000_000)
    duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    warmup_requests: int = Field(default=0, ge=0, le=10000)
    request_timeout_seconds: float = Field(default=5, gt=0, le=300)
    max_samples: int = Field(default=100000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def workload_mode(self) -> Self:
        if self.requests is not None and self.duration_seconds is not None:
            raise ValueError("Specify requests or duration_seconds, not both")
        if self.requests is None and self.duration_seconds is None:
            self.requests = 100
        if self.requests is not None and self.requests > self.max_samples:
            raise ValueError("requests exceeds max_samples")
        return self


class Assertions(StrictModel):
    max_failed_requests: int = Field(default=0, ge=0)
    max_p95_ms: float | None = Field(default=None, gt=0)
    max_p99_ms: float | None = Field(default=None, gt=0)
    max_error_rate: float | None = Field(default=None, ge=0, le=1)
    max_validation_failures: int = Field(default=0, ge=0)
    recovery_within_seconds: float | None = Field(default=None, gt=0)
    preferred_backend: str | None = Field(default=None, min_length=1, max_length=128)
    preferred_backend_within_seconds: float | None = Field(default=None, gt=0)


class Suite(StrictModel):
    version: Literal[1]
    target: Target
    workload: Workload
    assertions: Assertions = Field(default_factory=Assertions)
    metadata: DeploymentMetadata = Field(default_factory=DeploymentMetadata)
    scenario: ScenarioConfig | None = None

    @model_validator(mode="after")
    def scenario_contract(self) -> Self:
        scenario = self.scenario
        policy = self.assertions
        if scenario is not None:
            duration = self.workload.duration_seconds
            if duration is None:
                raise ValueError("Scenarios require a duration workload")
            hold = 0 if isinstance(scenario, KubernetesRestart) else scenario.hold_seconds
            if scenario.at_seconds + hold >= duration:
                raise ValueError("Leave measured traffic before and after the scenario")
        if (policy.preferred_backend is None) != (policy.preferred_backend_within_seconds is None):
            raise ValueError("Preferred backend requires its recovery deadline")
        if policy.preferred_backend is not None and self.target.backend is None:
            raise ValueError("Preferred-backend recovery requires identity extraction")
        if policy.recovery_within_seconds is not None or policy.preferred_backend is not None:
            if scenario is None or isinstance(scenario, KubernetesRestart):
                raise ValueError("Recovery assertions require a reversible fault scenario")
        return self


class Expectations(StrictModel):
    status: int = Field(default=200, ge=200, le=599)
    checks: list[JsonCheck] = Field(default_factory=list, alias="json")


class InputCase(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    body: JsonValue = None
    expect: Expectations

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: JsonValue) -> JsonValue:
        finite_json(value)
        return value


@dataclass
class LoadedSuite:
    suite: Suite
    cases: list[InputCase]
    headers: dict[str, str]
    cases_sha256: str
    source: Path
    cases_path: Path


def load_suite(path: Path, url: str | None = None) -> LoadedSuite:
    """Preflight all cases, credentials and HTTP request encodings before any traffic."""
    try:
        source = path.resolve()
        if source.stat().st_size > 1_048_576:
            raise SuiteError("Suite exceeds the 1 MiB limit")
        data = tomllib.loads(source.read_text())
        if url is not None:
            data.setdefault("target", {})["url"] = url
        suite = Suite.model_validate(data)
        cases_path = (source.parent / suite.workload.cases).resolve()
        if cases_path.stat().st_size > 16_777_216:
            raise SuiteError("Cases exceed the 16 MiB limit")
        cases = []
        for number, line in enumerate(cases_path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                cases.append(InputCase.model_validate(parse_json(line)))
            except (ValueError, RecursionError) as exc:
                raise SuiteError(
                    f"Invalid input case on line {number}; check the case schema"
                ) from exc
            if len(cases) > 10000:
                raise SuiteError("At most 10000 cases are supported")
        if not cases or len({case.name for case in cases}) != len(cases):
            raise SuiteError("Provide at least one case and unique case names")
        headers = {}
        for name, setting in suite.target.headers.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise SuiteError("Invalid request header name")
            if isinstance(setting, HeaderEnv):
                value = os.getenv(setting.env)
                if not value:
                    raise SuiteError(f"Missing nonempty header environment variable: {setting.env}")
                value = setting.prefix + value
            else:
                value = setting
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise SuiteError("Request headers cannot contain control characters")
            if value != value.strip(" "):
                raise SuiteError("Request headers cannot start or end with whitespace")
            headers[name] = value
        if len({name.lower() for name in headers}) != len(headers):
            raise SuiteError("Duplicate case-insensitive request header names")
        if {name.lower() for name in headers} & {"content-length", "transfer-encoding", "host"}:
            raise SuiteError("HTTPX owns Host and body framing headers")
        # Validate header encoding locally; an invalid header must not fail mid-run.
        httpx.Headers(headers, encoding="ascii")
        fingerprint = hashlib.sha256(
            json.dumps(
                [case.model_dump(mode="json", by_alias=True, exclude_unset=True) for case in cases],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return LoadedSuite(suite, cases, headers, fingerprint, source, cases_path)
    except SuiteError:
        raise
    except ValidationError as exc:
        # Pydantic's normal exception string includes inputs (potential credentials).
        location = ".".join(str(item) for item in exc.errors()[0]["loc"][:2])
        raise SuiteError(f"Invalid suite field {location}; check the suite schema") from exc
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise SuiteError(
            f"Cannot load suite or cases ({type(exc).__name__}); check the files"
        ) from exc
