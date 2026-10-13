"""Reproducibility metadata and canonical hashes, without copying payloads or credentials."""

import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from bront.validation import StrictModel


class DeploymentMetadata(StrictModel):
    model_revision: str | None = Field(default=None, min_length=1, max_length=256)
    serving_revision: str | None = Field(default=None, min_length=1, max_length=256)
    architecture: str | None = Field(default=None, min_length=1, max_length=128)
    resources: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def useful_identifiers(self) -> Self:
        values = [self.model_revision, self.serving_revision, self.architecture]
        values += list(self.resources) + list(self.resources.values())
        values += list(self.labels) + list(self.labels.values())
        if any(value is not None and (not value.strip() or len(value) > 1024) for value in values):
            raise ValueError("Metadata identifiers must be nonempty and at most 1024 characters")
        if len(self.resources) > 100 or len(self.labels) > 100:
            raise ValueError("At most 100 resources and labels are supported")
        return self

    def missing(self) -> list[str]:
        return [
            name
            for name in ("model_revision", "serving_revision", "architecture", "resources")
            if not getattr(self, name)
        ]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def runner_identity() -> dict[str, str]:
    root = Path(__file__).parent
    source = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }
    return {
        "version": version("bront"),
        "source_sha256": digest(source),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "system": platform.system(),
        "httpx": version("httpx"),
        "pydantic": version("pydantic"),
    }


class RunProvenance(StrictModel):
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    contract: dict[str, JsonValue]
    contract_sha256: str
    runner: dict[str, str]
    deployment: DeploymentMetadata
    metadata_source: Literal["declared"] = "declared"
    missing_metadata: list[str]
