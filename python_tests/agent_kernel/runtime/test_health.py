"""Verifies for kernelhealthprobe startup/liveness/readiness behavior."""

from __future__ import annotations

import pytest

from agent_kernel.runtime.health import HealthStatus, KernelHealthProbe


def _check(status: HealthStatus, message: str = "ok") -> tuple[HealthStatus, str]:
    """Checks the test assertion condition."""
    return (status, message)


def test_register_duplicate_check_name_raises() -> None:
    """Verifies register duplicate check name raises."""
    probe = KernelHealthProbe()
    probe.register_check("db", lambda: _check(HealthStatus.OK))
    with pytest.raises(ValueError):
        probe.register_check("db", lambda: _check(HealthStatus.OK))


def test_liveness_fails_only_on_unhealthy() -> None:
    """Verifies liveness fails only on unhealthy."""
    probe = KernelHealthProbe()
    probe.register_check("a", lambda: _check(HealthStatus.OK))
    probe.register_check("b", lambda: _check(HealthStatus.DEGRADED, "slow"))
    result = probe.liveness()
    assert result["status"] == "ok"


def test_readiness_is_degraded_when_any_check_degraded() -> None:
    """Verifies readiness is degraded when any check degraded."""
    probe = KernelHealthProbe()
    probe.register_check("a", lambda: _check(HealthStatus.OK))
    probe.register_check("b", lambda: _check(HealthStatus.DEGRADED, "slow"))
    result = probe.readiness()
    assert result["status"] == "degraded"


def test_startup_uses_required_subset_only() -> None:
    """Verifies startup uses required subset only."""
    probe = KernelHealthProbe()
    probe.register_check("required_ok", lambda: _check(HealthStatus.OK), required_for_startup=True)
    probe.register_check("optional_bad", lambda: _check(HealthStatus.UNHEALTHY, "offline"))
    result = probe.startup()
    assert result["status"] == "ok"
    assert "required_ok" in result["checks"]
    assert "optional_bad" not in result["checks"]


def test_startup_fails_when_required_not_ok() -> None:
    """Verifies startup fails when required not ok."""
    probe = KernelHealthProbe()
    probe.register_check(
        "required_bad",
        lambda: _check(HealthStatus.DEGRADED, "warming"),
        required_for_startup=True,
    )
    result = probe.startup()
    assert result["status"] == "unhealthy"


def test_startup_without_required_checks_is_ok() -> None:
    """Verifies startup without required checks is ok."""
    probe = KernelHealthProbe()
    probe.register_check("optional", lambda: _check(HealthStatus.UNHEALTHY, "offline"))
    result = probe.startup()
    assert result["status"] == "ok"
    assert result["checks"] == {}


def test_check_exception_is_captured_as_unhealthy() -> None:
    """Verifies check exception is captured as unhealthy."""
    probe = KernelHealthProbe()

    def _boom() -> tuple[HealthStatus, str]:
        """Raises a test exception."""
        raise RuntimeError("boom")

    probe.register_check("failing", _boom)
    result = probe.readiness()
    assert result["status"] == "unhealthy"
    assert result["checks"]["failing"]["status"] == "unhealthy"
    assert "boom" in result["checks"]["failing"]["message"]
