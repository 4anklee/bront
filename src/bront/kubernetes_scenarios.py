"""Deployment-scoped JSON patches with identity, spec, and resource-version guards."""

import asyncio
import copy
import json
from contextlib import suppress
from uuid import uuid4

from bront.scenario_models import (
    KubernetesRestart,
    KubernetesScaleZero,
    ScenarioError,
    ScenarioEvidence,
)

OWNER = "bront.dev/run-id"
RESTART = "bront.dev/restart-id"


class KubernetesDriver:
    def __init__(
        self, config: KubernetesRestart | KubernetesScaleZero, evidence: ScenarioEvidence
    ) -> None:
        self.config = config
        self.evidence = evidence
        self.reversible = isinstance(config, KubernetesScaleZero)
        self.token = uuid4().hex
        self.original: dict = {}
        self.changed_spec: dict = {}
        self.before_pods: set[str] = set()
        self.attempted = False

    async def command(self, *args: str, payload: list | None = None) -> dict:
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            "--context",
            self.config.context,
            "--namespace",
            self.config.namespace,
            f"--request-timeout={self.config.control_timeout_seconds:g}s",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self.config.control_timeout_seconds):
                stdout, _ = await process.communicate(
                    json.dumps(payload).encode() if payload is not None else None
                )
            if process.returncode:
                raise ScenarioError("kubectl_command_failed")
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def deployment(self) -> dict:
        current = await self.command("get", "deployment", self.config.deployment, "-o", "json")
        if self.original and current["metadata"]["uid"] != self.original["metadata"]["uid"]:
            raise ScenarioError("deployment_identity_changed")
        if current["metadata"].get("deletionTimestamp"):
            raise ScenarioError("deployment_is_being_deleted")
        return current

    async def pods(self, deployment: dict) -> list[dict]:
        sets = await self.command("get", "replicasets", "-o", "json")
        owned_sets = {
            item["metadata"]["uid"]
            for item in sets["items"]
            if any(
                ref.get("controller")
                and ref.get("uid") == deployment["metadata"]["uid"]
                and ref.get("kind") == "Deployment"
                for ref in item["metadata"].get("ownerReferences", [])
            )
        }
        pods = await self.command("get", "pods", "-o", "json")
        return [
            item
            for item in pods["items"]
            if any(
                ref.get("controller")
                and ref.get("uid") in owned_sets
                and ref.get("kind") == "ReplicaSet"
                for ref in item["metadata"].get("ownerReferences", [])
            )
        ]

    @staticmethod
    def ready(deployment: dict, pods: list[dict], replicas: int) -> bool:
        status = deployment.get("status", {})
        return (
            status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            and all(
                status.get(key, 0) == replicas
                for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
            and len(pods) == replicas
            and all(
                not pod["metadata"].get("deletionTimestamp")
                and any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
                for pod in pods
            )
        )

    def patch_operations(self, current: dict, spec: dict, *, restoring: bool = False) -> list:
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": self.original["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec", "value": current["spec"]},
            {"op": "replace", "path": "/spec", "value": spec},
        ]
        if self.reversible:
            annotations = dict(current["metadata"].get("annotations", {}))
            if restoring:
                annotations.pop(OWNER, None)
            else:
                annotations[OWNER] = self.token
            operations.append({"op": "add", "path": "/metadata/annotations", "value": annotations})
        return operations

    async def patch(
        self, current: dict, spec: dict, *, dry_run: bool = False, restoring: bool = False
    ) -> None:
        args = [
            "patch",
            "deployment",
            self.config.deployment,
            "--type=json",
            "--patch-file=/dev/stdin",
            "-o",
            "json",
        ]
        if dry_run:
            args.append("--dry-run=server")
        await self.command(*args, payload=self.patch_operations(current, spec, restoring=restoring))

    async def preflight(self) -> None:
        current = await self.deployment()
        self.original = copy.deepcopy(current)
        replicas = self.config.expected_replicas
        if current["spec"].get("replicas", 1) != replicas:
            raise ScenarioError("unexpected_replica_count")
        if current["spec"].get("paused"):
            raise ScenarioError("deployment_is_paused")
        if current["metadata"].get("annotations", {}).get(OWNER):
            raise ScenarioError("deployment_has_an_owned_fault")
        if (
            not self.reversible
            and current["spec"].get("strategy", {}).get("type") != "RollingUpdate"
        ):
            raise ScenarioError("rolling_update_strategy_required")
        pods = await self.pods(current)
        if not self.ready(current, pods, replicas):
            raise ScenarioError("deployment_must_start_fully_ready")
        self.before_pods = {pod["metadata"]["uid"] for pod in pods}
        self.changed_spec = copy.deepcopy(current["spec"])
        if self.reversible:
            self.changed_spec["replicas"] = 0
        else:
            metadata = self.changed_spec["template"].setdefault("metadata", {})
            metadata.setdefault("annotations", {})[RESTART] = self.token
        # Server dry-run verifies patch permission and admission before any mutation.
        await self.patch(current, self.changed_spec, dry_run=True)
        self.evidence.observations.update(
            deployment_uid=current["metadata"]["uid"],
            original_replicas=replicas,
            pods_before=[uid for uid in sorted(self.before_pods)],
            operation_id=self.token,
        )

    async def apply(self) -> None:
        current = await self.deployment()
        if current["spec"] != self.original["spec"] or current["metadata"].get(
            "annotations", {}
        ).get(OWNER):
            raise ScenarioError("deployment_changed_before_apply")
        self.attempted = True
        await self.patch(current, self.changed_spec)

    async def observe(self, *, restored: bool = False) -> None:
        while True:
            current = await self.deployment()
            expected = self.original["spec"] if restored else self.changed_spec
            if current["spec"] != expected:
                raise ScenarioError("deployment_spec_changed")
            if self.reversible:
                owner = current["metadata"].get("annotations", {}).get(OWNER)
                if owner != (None if restored else self.token):
                    raise ScenarioError("deployment_fault_ownership_changed")
            pods = await self.pods(current)
            replicas = self.config.expected_replicas if restored or not self.reversible else 0
            ready = self.ready(current, pods, replicas)
            ids = {pod["metadata"]["uid"] for pod in pods}
            if ready and (self.reversible or not (ids & self.before_pods)):
                key = "pods_restored" if restored else "pods_after"
                self.evidence.observations[key] = sorted(ids)
                return
            if any(
                c.get("type") == "Progressing" and c.get("status") == "False"
                for c in current.get("status", {}).get("conditions", [])
            ):
                raise ScenarioError("deployment_rollout_failed")
            await asyncio.sleep(0.25)

    async def restore(self) -> None:
        if not self.reversible or not self.attempted:
            return
        current = await self.deployment()
        owner = current["metadata"].get("annotations", {}).get(OWNER)
        if current["spec"] == self.original["spec"] and owner is None:
            return  # Apply never took effect, or restoration acknowledgement was lost.
        if owner != self.token or current["spec"] != self.changed_spec:
            raise ScenarioError("deployment_restore_conflict")
        await self.patch(current, self.original["spec"], restoring=True)
