import asyncio
import copy

import pytest

from bront.kubernetes_scenarios import OWNER, RESTART, KubernetesDriver
from bront.scenario_models import (
    KubernetesRestart,
    KubernetesScaleZero,
    ScenarioError,
    ScenarioEvidence,
)


def config(restart=False):
    values = dict(
        context="test-context",
        namespace="test-namespace",
        deployment="predictor",
        expected_replicas=2,
        at_seconds=0.01,
        control_timeout_seconds=0.1,
        observe_timeout_seconds=0.1,
    )
    if restart:
        return KubernetesRestart.model_validate({**values, "kind": "kubernetes-restart"})
    return KubernetesScaleZero.model_validate(
        {**values, "kind": "kubernetes-scale-zero", "hold_seconds": 0.02}
    )


class Cluster(KubernetesDriver):
    def __init__(self, restart=False):
        settings = config(restart)
        super().__init__(settings, ScenarioEvidence(kind=settings.kind, configuration={}))
        self.object: dict = {
            "metadata": {
                "uid": "deployment-uid",
                "generation": 1,
                "resourceVersion": "1",
                "annotations": {"other-owner": "preserve"},
            },
            "spec": {
                "replicas": 2,
                "strategy": {"type": "RollingUpdate"},
                "template": {"metadata": {}, "spec": {"containers": [{"image": "model:v1"}]}},
            },
        }
        self.ids = ["old-1", "old-2"]
        self.calls = []
        self.mutations = 0
        self.deny_patch = False
        self.lose_ack = False
        self.replace_pods = True

    async def command(self, *args, payload=None):
        self.calls.append(args)
        if args[:2] == ("get", "deployment"):
            result = copy.deepcopy(self.object)
            count = result["spec"]["replicas"]
            result["status"] = {
                "observedGeneration": result["metadata"]["generation"],
                "replicas": count,
                "updatedReplicas": count,
                "readyReplicas": count,
                "availableReplicas": count,
            }
            return result
        if args[:2] == ("get", "replicasets"):
            return {
                "items": [
                    {
                        "metadata": {
                            "uid": "owned-rs",
                            "ownerReferences": [
                                {"kind": "Deployment", "controller": True, "uid": "deployment-uid"}
                            ],
                        }
                    }
                ]
            }
        if args[:2] == ("get", "pods"):
            pods = [
                {
                    "metadata": {
                        "uid": uid,
                        "ownerReferences": [
                            {"kind": "ReplicaSet", "controller": True, "uid": "owned-rs"}
                        ],
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
                for uid in self.ids
            ]
            pods.append(
                {
                    "metadata": {
                        "uid": "unrelated-pod",
                        "ownerReferences": [
                            {"kind": "ReplicaSet", "controller": True, "uid": "unrelated-rs"}
                        ],
                    }
                }
            )
            return {"items": pods}
        assert args[:2] == ("patch", "deployment")
        assert args[2] == "predictor" and "--patch-file=/dev/stdin" in args
        if self.deny_patch:
            raise ScenarioError("kubectl_command_failed")
        updated = copy.deepcopy(self.object)
        assert payload is not None
        for operation in payload:
            path = operation["path"].strip("/").split("/")
            parent = updated
            for key in path[:-1]:
                parent = parent[key]
            if operation["op"] == "test":
                if parent[path[-1]] != operation["value"]:
                    raise ScenarioError("kubectl_command_failed")
            else:
                parent[path[-1]] = copy.deepcopy(operation["value"])
        if "--dry-run=server" in args:
            return updated
        self.mutations += 1
        updated["metadata"]["generation"] += 1
        updated["metadata"]["resourceVersion"] = str(
            int(updated["metadata"]["resourceVersion"]) + 1
        )
        self.object = updated
        if self.replace_pods:
            self.ids = [f"new-{self.mutations}-{i}" for i in range(updated["spec"]["replicas"])]
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("secret-kubectl-output")
        return updated


async def test_scale_zero_restores_exact_spec_and_preserves_unrelated_objects():
    driver = Cluster()
    original = copy.deepcopy(driver.object["spec"])
    await driver.preflight()
    assert driver.mutations == 0  # Preflight used only server dry-run.
    await driver.apply()
    await driver.observe()
    assert driver.object["spec"]["replicas"] == 0
    assert driver.object["metadata"]["annotations"][OWNER] == driver.token
    driver.object["metadata"]["annotations"]["new-owner"] = "preserve-too"
    await driver.restore()
    await driver.observe(restored=True)
    assert driver.object["spec"] == original
    assert driver.object["metadata"]["annotations"] == {
        "other-owner": "preserve",
        "new-owner": "preserve-too",
    }
    assert driver.evidence.observations["pods_before"] == ["old-1", "old-2"]
    assert driver.evidence.observations["pods_after"] == []
    restored = driver.evidence.observations["pods_restored"]
    assert isinstance(restored, list) and len(restored) == 2


async def test_restart_requires_actual_replacement_and_is_not_rolled_back():
    driver = Cluster(restart=True)
    await driver.preflight()
    await driver.apply()
    await driver.observe()
    assert driver.object["spec"]["template"]["metadata"]["annotations"][RESTART] == driver.token
    assert driver.evidence.observations["pods_after"] == ["new-1-0", "new-1-1"]
    await driver.restore()
    assert driver.mutations == 1
    assert not driver.reversible


async def test_ready_counts_alone_do_not_prove_replacement():
    driver = Cluster(restart=True)
    driver.replace_pods = False
    await driver.preflight()
    await driver.apply()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(driver.observe(), 0.01)


@pytest.mark.parametrize("change", ["uid", "spec", "owner"])
async def test_restore_refuses_concurrent_change(change):
    driver = Cluster()
    await driver.preflight()
    await driver.apply()
    if change == "uid":
        driver.object["metadata"]["uid"] = "replacement-deployment"
    elif change == "spec":
        driver.object["spec"]["replicas"] = 3
    else:
        driver.object["metadata"]["annotations"][OWNER] = "another-run"
    before = copy.deepcopy(driver.object)
    with pytest.raises(ScenarioError):
        await driver.restore()
    assert driver.object == before
    assert driver.mutations == 1


async def test_change_between_preflight_and_apply_is_rejected():
    driver = Cluster()
    await driver.preflight()
    driver.object["spec"]["template"]["spec"]["containers"][0]["image"] = "model:v2"
    with pytest.raises(ScenarioError, match="changed_before_apply"):
        await driver.apply()
    assert not driver.attempted
    assert driver.mutations == 0


async def test_atomic_patch_tests_reject_stale_resource_version():
    driver = Cluster()
    await driver.preflight()
    snapshot = await driver.deployment()
    driver.object["metadata"]["resourceVersion"] = "2"
    with pytest.raises(ScenarioError):
        await driver.patch(snapshot, driver.changed_spec)
    assert driver.mutations == 0


async def test_lost_mutation_ack_is_restored_using_ownership():
    driver = Cluster()
    await driver.preflight()
    driver.lose_ack = True
    with pytest.raises(TimeoutError):
        await driver.apply()
    assert driver.attempted
    await driver.restore()
    await driver.observe(restored=True)
    assert driver.object["spec"]["replicas"] == 2


async def test_lost_restore_ack_is_idempotently_verified():
    driver = Cluster()
    await driver.preflight()
    await driver.apply()
    driver.lose_ack = True
    with pytest.raises(TimeoutError):
        await driver.restore()
    await driver.restore()
    await driver.observe(restored=True)
    assert driver.mutations == 2


@pytest.mark.parametrize("problem", ["replicas", "paused", "ownership", "permission", "strategy"])
async def test_preflight_rejects_unsupported_state_before_mutation(problem):
    driver = Cluster(restart=True)
    if problem == "replicas":
        driver.object["spec"]["replicas"] = 1
    elif problem == "paused":
        driver.object["spec"]["paused"] = True
    elif problem == "ownership":
        driver.object["metadata"]["annotations"][OWNER] = "existing-fault"
    elif problem == "permission":
        driver.deny_patch = True
    else:
        driver.object["spec"]["strategy"]["type"] = "Recreate"
    with pytest.raises(ScenarioError):
        await driver.preflight()
    assert driver.mutations == 0


async def test_kubectl_uses_explicit_scope_and_reaps_cancelled_process(monkeypatch):
    called = []
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, payload):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9

    process = Process()

    async def spawn(*args, **kwargs):
        called.extend(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    settings = config()
    driver = KubernetesDriver(settings, ScenarioEvidence(kind=settings.kind, configuration={}))
    task = asyncio.create_task(driver.deployment())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert called[:5] == ["kubectl", "--context", "test-context", "--namespace", "test-namespace"]
    assert process.killed and process.waited
