"""HTTP JSON target adapter; it has no gateway or model-specific dependencies."""

import json
from typing import Literal

import httpx

from bront.suites import InputCase, LoadedSuite
from bront.validation import StrictModel, at_pointer, parse_json, validate_json


class Evaluation(StrictModel):
    error: Literal["http_status", "validation"] | None = None
    reason: str | None = None
    check_index: int | None = None
    backend: str | None = None


class HttpJsonTarget:
    def __init__(self, loaded: LoadedSuite) -> None:
        self.target = loaded.suite.target
        self.cases = loaded.cases
        self.headers = loaded.headers
        self.bodies = {
            case.name: json.dumps(case.body, allow_nan=False, separators=(",", ":")).encode()
            if "body" in case.model_fields_set
            else None
            for case in self.cases
        }

    async def send(self, client: httpx.AsyncClient, case: InputCase) -> httpx.Response:
        headers = dict(self.headers)
        body = self.bodies[case.name]
        if body is not None and not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
        return await client.request(
            self.target.method,
            str(self.target.url),
            headers=headers,
            content=body,
            follow_redirects=False,
        )

    def evaluate(self, response: httpx.Response, case: InputCase) -> Evaluation:
        if response.status_code != case.expect.status:
            return Evaluation(error="http_status", reason="unexpected_status")
        identity = self.target.backend
        value = None
        if case.expect.checks or (identity is not None and identity.pointer is not None):
            try:
                value = parse_json(response.content)
            except (ValueError, UnicodeError, RecursionError):
                if case.expect.checks:
                    return Evaluation(error="validation", reason="invalid_json")
                # Optional identity extraction must not turn status-only checks into failures.
        result = validate_json(value, case.expect.checks)
        if not result.passed:
            return Evaluation(
                error="validation", reason=result.reason, check_index=result.check_index
            )
        backend = None
        if identity is not None:
            if identity.header is not None:
                backend = response.headers.get(identity.header)
            elif identity.pointer is not None:
                try:
                    backend = at_pointer(value, identity.pointer)
                except (KeyError, IndexError, ValueError):
                    pass
        return Evaluation(
            backend=backend if isinstance(backend, str) and 0 < len(backend) <= 128 else None
        )
