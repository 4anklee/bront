"""Read observed gateway state and explicitly probe or fault demo backends."""

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from inference_guard.adapters import MockAdapter, TritonAdapter
from inference_guard.benchmark import GatewayConfiguration
from inference_guard.faults import FaultController
from inference_guard.schemas import InferenceRequest

DEMO_TOXIC = "inferenceguard-demo-latency"


class FaultAction(StrEnum):
    outage = "outage"
    delay = "delay"
    reset = "reset"


class CircuitObservation(BaseModel):
    state: Literal["closed", "open", "half_open"]
    consecutive_failures: int = Field(ge=0)


class ProbeResult(BaseModel):
    url: str
    protocol: Literal["mock", "triton"]
    healthy: bool
    elapsed_ms: float
    error: str | None = None


class StatusReport(BaseModel):
    observed_at: str
    gateway_url: str
    instance: str | None = None
    ready: bool
    configuration: GatewayConfiguration
    backends: dict[str, CircuitObservation]
    active_checks: dict[str, ProbeResult] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.ready and all(check.healthy for check in self.active_checks.values())


async def probe_backend(
    client: httpx.AsyncClient, url: str, protocol: Literal["mock", "triton"], timeout_seconds: float
) -> ProbeResult:
    """Check the double contract directly; this does not update gateway circuits."""
    request = InferenceRequest(model="double", inputs=[1, 2, 3])
    adapter = TritonAdapter() if protocol == "triton" else MockAdapter()
    encoded = adapter.encode(request)
    started = monotonic()
    error = None
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.post(
                url.rstrip("/") + encoded.path,
                json=encoded.json,
                content=encoded.content,
                headers=encoded.headers,
            )
            response.raise_for_status()
            result = adapter.decode(response, request)
            if result.model != "double" or result.outputs != [2, 4, 6]:
                raise ValueError("Incompatible inference output (expected double: [2, 4, 6])")
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return ProbeResult(
        url=url,
        protocol=protocol,
        healthy=error is None,
        elapsed_ms=(monotonic() - started) * 1000,
        error=error,
    )


async def inspect_status(
    client: httpx.AsyncClient,
    gateway_url: str,
    primary_url: str | None = None,
    fallback_url: str | None = None,
    timeout_seconds: float = 5,
) -> StatusReport:
    async with asyncio.timeout(timeout_seconds):
        response = await client.get(gateway_url.rstrip("/") + "/status")
        response.raise_for_status()
        data = response.json()
        readiness = await client.get(gateway_url.rstrip("/") + "/readyz")
    report = StatusReport(
        observed_at=datetime.now(UTC).isoformat(),
        gateway_url=gateway_url,
        instance=data.get("instance"),
        ready=readiness.status_code == 200,
        configuration=data["configuration"],
        backends=data["backends"],
    )
    if set(report.backends) != {"primary", "fallback"}:
        raise ValueError("Gateway status must contain primary and fallback observations")
    checks: list[tuple[str, str | None, Literal["mock", "triton"]]] = [
        ("primary", primary_url, report.configuration.primary_protocol),
        ("fallback", fallback_url, "mock"),
    ]
    # Explicit calls keep the protocol type narrow and both checks concurrent.
    results = await asyncio.gather(
        *(
            probe_backend(client, url, protocol, timeout_seconds)
            for name, url, protocol in checks
            if url is not None
        )
    )
    report.active_checks = dict(
        zip(
            [name for name, url, _ in checks if url is not None],
            results,
            strict=True,
        )
    )
    return report


async def control_fault(
    client: httpx.AsyncClient, url: str, action: FaultAction, latency_ms: int = 2000
) -> None:
    controller = FaultController(client, url, toxic_name=DEMO_TOXIC)
    if action == FaultAction.reset:
        # An explicit reset enables the demo proxy and deletes only our named delay.
        # It also repairs a prior injection whose HTTP acknowledgement was lost.
        controller.disabled = controller.delayed = True
        await controller.restore()
    else:
        await controller.preflight()
        if action == FaultAction.outage:
            await controller.outage()
        else:
            await controller.delay(latency_ms)
