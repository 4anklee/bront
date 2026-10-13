"""Reuse scoped Toxiproxy mutations, checking topology before touching a proxy."""

import httpx

from inference_guard.faults import FaultController
from inference_guard.scenario_models import ProxyDelay, ProxyOutage, ScenarioError, ScenarioEvidence


class ProxyDriver:
    reversible = True

    def __init__(
        self,
        config: ProxyOutage | ProxyDelay,
        evidence: ScenarioEvidence,
        client: httpx.AsyncClient,
    ) -> None:
        self.config = config
        self.evidence = evidence
        self.controller = FaultController(client, str(config.url), proxy=config.proxy)
        self.identity: dict = {}
        self.attempted = False

    async def state(self) -> dict:
        response = await self.controller.client.get(self.controller.url)
        response.raise_for_status()
        state = response.json()
        if not isinstance(state, dict) or not isinstance(state.get("toxics"), list):
            raise ScenarioError("invalid_proxy_state")
        identity = {key: state.get(key) for key in ("name", "listen", "upstream")}
        if any(not isinstance(value, str) for value in identity.values()):
            raise ScenarioError("invalid_proxy_identity")
        if self.identity and self.identity != identity:
            raise ScenarioError("proxy_topology_changed")
        return state

    async def preflight(self) -> None:
        state = await self.state()
        if state.get("enabled") is not True or state["toxics"]:
            raise ScenarioError("proxy_must_start_enabled_without_faults")
        self.identity = {key: state[key] for key in ("name", "listen", "upstream")}
        self.evidence.observations.update(
            proxy=self.config.proxy, original_enabled=True, toxic_name=self.controller.toxic_name
        )

    async def apply(self) -> None:
        state = await self.state()
        if state.get("enabled") is not True or state["toxics"]:
            raise ScenarioError("proxy_changed_before_apply")
        self.attempted = True  # A lost acknowledgement still requires cleanup.
        if isinstance(self.config, ProxyDelay):
            await self.controller.delay(self.config.latency_ms)
        else:
            await self.controller.outage()

    async def observe(self, *, restored: bool = False) -> None:
        state = await self.state()
        owned = [t for t in state["toxics"] if t.get("name") == self.controller.toxic_name]
        others = [t for t in state["toxics"] if t.get("name") != self.controller.toxic_name]
        if others:
            raise ScenarioError("unrelated_proxy_fault_detected")
        if restored:
            matched = state.get("enabled") is True and not owned
        elif isinstance(self.config, ProxyDelay):
            matched = (
                state.get("enabled") is True
                and len(owned) == 1
                and owned[0].get("type") == "latency"
                and owned[0].get("attributes", {}).get("latency") == self.config.latency_ms
            )
        else:
            matched = state.get("enabled") is False
        if not matched:
            raise ScenarioError("proxy_state_not_observed")

    async def restore(self) -> None:
        if not self.attempted:
            return
        # Refuse to enable a different/reconfigured proxy. Never use the global reset API.
        await self.state()
        await self.controller.restore()
