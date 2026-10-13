from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model: Literal["double"] = "double"
    inputs: list[float] = Field(min_length=1, max_length=1024)


class BackendResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    model: Literal["double"]
    outputs: list[float] = Field(min_length=1, max_length=1024)


class InferenceResponse(BackendResponse):
    backend: str


class GatewayConfiguration(BaseModel):
    primary_protocol: Literal["mock", "triton"]
    request_timeout_seconds: float = Field(gt=0)
    failure_threshold: int = Field(ge=1)
    recovery_timeout_seconds: float = Field(gt=0)
