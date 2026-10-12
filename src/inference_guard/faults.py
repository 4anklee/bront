"""Controls only the benchmark proxy; never stops the inference service."""

import asyncio
from uuid import uuid4

import httpx


class FaultController:
    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self.client = client
        self.url = url.rstrip("/") + "/proxies/primary"
        self.toxic_name = "inferenceguard-" + uuid4().hex
        self.disabled = False
        self.delayed = False

    async def preflight(self) -> None:
        response = await self.client.get(self.url)
        response.raise_for_status()
        proxy = response.json()
        if not isinstance(proxy, dict) or not proxy.get("enabled") or proxy.get("toxics"):
            raise ValueError("Benchmark proxy must be enabled with no existing faults")

    async def outage(self) -> None:
        # Mark before the request: even a lost HTTP response must trigger restoration.
        self.disabled = True
        response = await self.client.post(self.url, json={"enabled": False})
        response.raise_for_status()

    async def delay(self, milliseconds: int) -> None:
        self.delayed = True
        response = await self.client.post(
            self.url + "/toxics",
            json={
                "name": self.toxic_name,
                "type": "latency",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"latency": milliseconds, "jitter": 0},
            },
        )
        response.raise_for_status()

    async def _enable(self) -> None:
        response = await self.client.post(self.url, json={"enabled": True})
        response.raise_for_status()
        self.disabled = False

    async def _remove_delay(self) -> None:
        response = await self.client.delete(self.url + "/toxics/" + self.toxic_name)
        if response.status_code != 404:
            response.raise_for_status()
        self.delayed = False

    async def restore(self) -> None:
        actions = []
        if self.disabled:
            actions.append(self._enable())
        if self.delayed:
            actions.append(self._remove_delay())
        # Attempt both repairs, even if the control API fails during one of them.
        results = await asyncio.gather(*actions, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise RuntimeError(
                    "Could not restore benchmark proxy; check Toxiproxy before another run"
                ) from result
