"""Scenario configuration and evidence shared by control drivers and the runner."""

from typing import Annotated, Literal

from pydantic import Field, HttpUrl, JsonValue, field_validator

from bront.validation import StrictModel


class ScenarioError(RuntimeError):
    """Static, safe-to-report scenario failure code (never raw control output)."""


class Timing(StrictModel):
    at_seconds: float = Field(gt=0, le=3600)
    control_timeout_seconds: float = Field(default=10, gt=0, le=60)
    observe_timeout_seconds: float = Field(default=60, gt=0, le=300)


class ProxySettings(Timing):
    url: HttpUrl
    proxy: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    hold_seconds: float = Field(gt=0, le=3600)

    @field_validator("url")
    @classmethod
    def control_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Control URL must not contain credentials, query, or fragment")
        return value


class ProxyOutage(ProxySettings):
    kind: Literal["proxy-outage"]


class ProxyDelay(ProxySettings):
    kind: Literal["proxy-delay"]
    latency_ms: int = Field(ge=1, le=300000)


class KubernetesSettings(Timing):
    context: str = Field(min_length=1, pattern=r"^[^\s-][^\s]*$")
    namespace: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    deployment: str = Field(pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
    expected_replicas: int = Field(ge=1, le=10000)


class KubernetesRestart(KubernetesSettings):
    kind: Literal["kubernetes-restart"]


class KubernetesScaleZero(KubernetesSettings):
    kind: Literal["kubernetes-scale-zero"]
    hold_seconds: float = Field(gt=0, le=3600)


type ScenarioConfig = Annotated[
    ProxyOutage | ProxyDelay | KubernetesRestart | KubernetesScaleZero,
    Field(discriminator="kind"),
]


class ScenarioEvent(StrictModel):
    action: str
    scheduled_ms: float | None = None
    started_ms: float
    finished_ms: float | None = None
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    error: str | None = None


class RecoveryEvidence(StrictModel):
    from_ms: float
    first_valid_response_ms: float | None = None
    backend_identity_available: bool
    preferred_backend: str | None = None
    first_preferred_backend_ms: float | None = None


class ScenarioEvidence(StrictModel):
    kind: str
    clock_origin: Literal["preflight", "measurement"] = "preflight"
    configuration: dict[str, JsonValue]
    # Only identifiers and counts belong here, never entire Kubernetes objects.
    observations: dict[str, JsonValue] = Field(default_factory=dict)
    events: list[ScenarioEvent] = Field(default_factory=list)
    cleanup: Literal["not_required", "pending", "restored", "failed", "not_reversible"] = (
        "not_required"
    )
    completed: bool = False
    recovery: RecoveryEvidence | None = None
