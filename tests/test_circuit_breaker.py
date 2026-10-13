from bront.reference.circuit_breaker import CircuitBreaker


def test_threshold_cooldown_and_single_recovery_probe():
    now = [0.0]
    breaker = CircuitBreaker(2, 10, clock=lambda: now[0])
    first = breaker.acquire()
    assert first is not None
    breaker.fail(first)
    assert breaker.state == "closed"
    second = breaker.acquire()
    assert second is not None
    breaker.fail(second)
    assert breaker.acquire() is None
    now[0] = 10
    probe = breaker.acquire()
    assert probe is not None
    assert breaker.acquire() is None
    breaker.succeed(probe)
    assert breaker.state == "closed"
    assert breaker.failures == 0


def test_late_success_cannot_close_open_circuit():
    breaker = CircuitBreaker(1, 10)
    first = breaker.acquire()
    second = breaker.acquire()
    assert first is not None
    assert second is not None
    breaker.fail(first)
    breaker.succeed(second)
    assert breaker.state == "open"


def test_failed_probe_restarts_cooldown():
    now = [0.0]
    breaker = CircuitBreaker(1, 10, clock=lambda: now[0])
    first = breaker.acquire()
    assert first is not None
    breaker.fail(first)
    now[0] = 10
    probe = breaker.acquire()
    assert probe is not None
    breaker.fail(probe)
    now[0] = 19
    assert breaker.acquire() is None
    now[0] = 20
    assert breaker.acquire() is not None
