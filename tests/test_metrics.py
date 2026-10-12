from inference_guard.circuit_breaker import CircuitBreaker
from inference_guard.metrics import Metrics


def test_circuit_metrics_include_failed_probes_but_ignore_stale_completions():
    now = [0.0]
    metrics = Metrics()
    breaker = CircuitBreaker(1, 10, lambda: now[0], metrics.watch_circuit("primary"))
    value = metrics.registry.get_sample_value

    def state(name):
        return value("inferenceguard_circuit_state", {"backend": "primary", "state": name})

    assert state("closed") == 1
    assert value("inferenceguard_circuit_last_recovery_seconds", {"backend": "primary"}) is None
    ticket = breaker.acquire()
    assert ticket is not None
    breaker.fail(ticket)
    breaker.succeed(ticket)  # Completion from the old generation must not record recovery.
    assert state("open") == 1
    now[0] = 10
    probe = breaker.acquire()
    assert probe is not None
    assert state("half_open") == 1
    breaker.fail(probe)
    now[0] = 20
    probe = breaker.acquire()
    assert probe is not None
    now[0] = 21
    breaker.succeed(probe)
    assert [state(s) for s in ("closed", "open", "half_open")] == [1, 0, 0]
    assert value("inferenceguard_circuit_last_recovery_seconds", {"backend": "primary"}) == 21
    assert (
        value("inferenceguard_circuit_recovery_duration_seconds_count", {"backend": "primary"}) == 1
    )
    assert (
        value(
            "inferenceguard_circuit_transitions_total",
            {
                "backend": "primary",
                "from_state": "open",
                "to_state": "half_open",
            },
        )
        == 2
    )
    # A second episode measures independently and does not erase the last result on opening.
    now[0] = 30
    ticket = breaker.acquire()
    assert ticket is not None
    breaker.fail(ticket)
    now[0] = 40
    probe = breaker.acquire()
    assert probe is not None
    breaker.succeed(probe)
    assert value("inferenceguard_circuit_last_recovery_seconds", {"backend": "primary"}) == 10
    assert (
        value("inferenceguard_circuit_recovery_duration_seconds_sum", {"backend": "primary"}) == 31
    )
