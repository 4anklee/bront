"""Translate the public contract into each backend's wire protocol."""

import json
import struct
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field, StrictInt

from bront.reference.schemas import BackendResponse, InferenceRequest


@dataclass
class EncodedRequest:
    path: str
    json: dict[str, object] | None = None
    content: bytes | None = None
    headers: dict[str, str] | None = None


class BackendAdapter(Protocol):
    def encode(self, request: InferenceRequest) -> EncodedRequest: ...

    def decode(self, response: httpx.Response, request: InferenceRequest) -> BackendResponse: ...


class MockAdapter:
    def encode(self, request: InferenceRequest) -> EncodedRequest:
        return EncodedRequest("/infer", json=request.model_dump())

    def decode(self, response: httpx.Response, request: InferenceRequest) -> BackendResponse:
        return BackendResponse.model_validate(response.json())


class BinaryParameters(BaseModel):
    binary_data_size: StrictInt = Field(ge=8, le=8192)


class TritonOutput(BaseModel):
    name: Literal["OUTPUT"]
    datatype: Literal["FP64"]
    shape: list[StrictInt] = Field(min_length=1, max_length=1)
    parameters: BinaryParameters


class TritonResponse(BaseModel):
    model_name: Literal["double"]
    model_version: Literal["1"]
    outputs: list[TritonOutput] = Field(min_length=1, max_length=1)


class TritonAdapter:
    def encode(self, request: InferenceRequest) -> EncodedRequest:
        # Binary FP64 avoids a second decimal-to-float conversion inside Triton.
        data = struct.pack(f"<{len(request.inputs)}d", *request.inputs)
        metadata = {
            "inputs": [
                {
                    "name": "INPUT",
                    "shape": [len(request.inputs)],
                    "datatype": "FP64",
                    "parameters": {"binary_data_size": len(data)},
                }
            ],
            "outputs": [{"name": "OUTPUT", "parameters": {"binary_data": True}}],
        }
        header = json.dumps(metadata, separators=(",", ":")).encode()
        return EncodedRequest(
            "/v2/models/double/versions/1/infer",
            content=header + data,
            headers={
                "Content-Type": "application/octet-stream",
                "Inference-Header-Content-Length": str(len(header)),
            },
        )

    def decode(self, response: httpx.Response, request: InferenceRequest) -> BackendResponse:
        header_length = response.headers.get("Inference-Header-Content-Length", "")
        if not header_length.isdecimal():
            raise ValueError("Triton response is missing a valid binary header length")
        boundary = int(header_length)
        if not 0 < boundary < len(response.content):
            raise ValueError("Triton response has an invalid binary header boundary")
        result = TritonResponse.model_validate(json.loads(response.content[:boundary]))
        output = result.outputs[0]
        count = len(request.inputs)
        data = response.content[boundary:]
        if (
            output.shape != [count]
            or output.parameters.binary_data_size != count * 8
            or len(data) != count * 8
        ):
            raise ValueError("Triton returned an incompatible output shape")
        return BackendResponse(model=request.model, outputs=list(struct.unpack(f"<{count}d", data)))
