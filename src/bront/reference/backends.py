import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

import httpx
from pydantic import ValidationError

from bront.reference.adapters import BackendAdapter, MockAdapter, TritonAdapter
from bront.reference.circuit_breaker import CircuitBreaker
from bront.reference.config import Settings
from bront.reference.metrics import Metrics
from bront.reference.schemas import InferenceRequest, InferenceResponse

logger = logging.getLogger(__name__)


class BackendsUnavailable(Exception):
    pass


@dataclass
class Backend:
    name: str
    url: str
    breaker: CircuitBreaker
    adapter: BackendAdapter


class BackendRouter:
    def __init__(self, client: httpx.AsyncClient, settings: Settings, metrics: Metrics) -> None:
        self.client = client
        self.timeout = settings.request_timeout_seconds
        self.metrics = metrics
        self.backends = [
            Backend(
                name,
                str(url).rstrip("/"),
                CircuitBreaker(
                    settings.failure_threshold,
                    settings.recovery_timeout_seconds,
                    on_transition=metrics.watch_circuit(name),
                ),
                TritonAdapter()
                if name == "primary" and settings.primary_protocol == "triton"
                else MockAdapter(),
            )
            for name, url in (
                ("primary", settings.primary_url),
                ("fallback", settings.fallback_url),
            )
        ]

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        for backend in self.backends:
            ticket = backend.breaker.acquire()
            if ticket is None:
                continue
            started = monotonic()
            try:
                # Bound the entire attempt, in addition to HTTPX's per-operation timeout.
                async with asyncio.timeout(self.timeout):
                    encoded = backend.adapter.encode(request)
                    response = await self.client.post(
                        f"{backend.url}{encoded.path}",
                        json=encoded.json,
                        content=encoded.content,
                        headers=encoded.headers,
                    )
                    response.raise_for_status()
                    result = backend.adapter.decode(response, request)
                    if len(result.outputs) != len(request.inputs):
                        raise ValueError("Backend returned the wrong output shape")
            except asyncio.CancelledError:
                # Release a half-open probe if the caller or server cancels this request.
                backend.breaker.fail(ticket)
                raise
            except (httpx.HTTPError, TimeoutError, ValidationError, ValueError) as exc:
                backend.breaker.fail(ticket)
                self.metrics.attempts.labels(backend.name, "error").inc()
                logger.warning(
                    "backend_failed backend=%s error=%s circuit=%s",
                    backend.name,
                    type(exc).__name__,
                    backend.breaker.state,
                )
            else:
                backend.breaker.succeed(ticket)
                self.metrics.attempts.labels(backend.name, "success").inc()
                self.metrics.requests.labels("success").inc()
                return InferenceResponse(**result.model_dump(), backend=backend.name)
            finally:
                self.metrics.latency.labels(backend.name).observe(monotonic() - started)

        self.metrics.requests.labels("unavailable").inc()
        raise BackendsUnavailable

    def status(self) -> dict[str, dict[str, str | int]]:
        return {backend.name: backend.breaker.snapshot() for backend in self.backends}
