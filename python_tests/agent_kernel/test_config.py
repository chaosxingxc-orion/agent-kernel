"""Tests for agent_kernel.config.KernelConfig."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import ClassVar

import pytest

from agent_kernel.config import KernelConfig


class TestDefaultValues:
    """Verify every default matches the historically hardcoded value."""

    def test_facade_max_tracked_runs(self) -> None:
        assert KernelConfig().max_tracked_runs == 10_000

    def test_runtime_max_retained_runs(self) -> None:
        assert KernelConfig().max_retained_runs == 5_000

    def test_local_gateway_max_turn_cache_size(self) -> None:
        assert KernelConfig().max_turn_cache_size == 5_000

    def test_http_port(self) -> None:
        assert KernelConfig().http_port == 8_400

    def test_max_request_body_bytes(self) -> None:
        assert KernelConfig().max_request_body_bytes == 1_048_576

    def test_api_key_default_none(self) -> None:
        assert KernelConfig().api_key is None

    def test_heartbeat_dispatching_timeout(self) -> None:
        assert KernelConfig().heartbeat_dispatching_timeout_s == 300

    def test_heartbeat_waiting_result_timeout(self) -> None:
        assert KernelConfig().heartbeat_waiting_result_timeout_s == 600

    def test_heartbeat_waiting_external_timeout(self) -> None:
        assert KernelConfig().heartbeat_waiting_external_timeout_s == 3_600

    def test_heartbeat_waiting_human_timeout(self) -> None:
        assert KernelConfig().heartbeat_waiting_human_timeout_s == 86_400

    def test_heartbeat_recovering_timeout(self) -> None:
        assert KernelConfig().heartbeat_recovering_timeout_s == 180

    def test_heartbeat_min_interval(self) -> None:
        assert KernelConfig().heartbeat_min_interval_s == 5

    def test_heartbeat_stale_check_age(self) -> None:
        assert KernelConfig().heartbeat_stale_check_age_s == 60

    def test_default_model_ref(self) -> None:
        assert KernelConfig().default_model_ref == "echo"

    def test_default_tenant_policy_ref(self) -> None:
        assert KernelConfig().default_tenant_policy_ref == "policy:default"

    def test_default_permission_mode(self) -> None:
        assert KernelConfig().default_permission_mode == "strict"

    def test_phase_timeout_default_none(self) -> None:
        assert KernelConfig().phase_timeout_s is None

    def test_circuit_breaker_threshold(self) -> None:
        assert KernelConfig().circuit_breaker_threshold == 5

    def test_circuit_breaker_half_open_ms(self) -> None:
        assert KernelConfig().circuit_breaker_half_open_ms == 30_000

    def test_history_reset_threshold(self) -> None:
        assert KernelConfig().history_reset_threshold == 10_000


class TestFromEnvOverrides:
    """Verify from_env() picks up environment variables correctly."""

    _ENV_OVERRIDES: ClassVar[dict[str, str]] = {
        "AGENT_KERNEL_MAX_TRACKED_RUNS": "500",
        "AGENT_KERNEL_MAX_RETAINED_RUNS": "250",
        "AGENT_KERNEL_MAX_TURN_CACHE_SIZE": "1000",
        "AGENT_KERNEL_HTTP_PORT": "9000",
        "AGENT_KERNEL_MAX_REQUEST_BODY_BYTES": "2097152",
        "AGENT_KERNEL_API_KEY": "secret-key-42",
        "AGENT_KERNEL_HEARTBEAT_DISPATCHING_TIMEOUT_S": "60",
        "AGENT_KERNEL_HEARTBEAT_WAITING_RESULT_TIMEOUT_S": "120",
        "AGENT_KERNEL_HEARTBEAT_WAITING_EXTERNAL_TIMEOUT_S": "7200",
        "AGENT_KERNEL_HEARTBEAT_WAITING_HUMAN_TIMEOUT_S": "43200",
        "AGENT_KERNEL_HEARTBEAT_RECOVERING_TIMEOUT_S": "90",
        "AGENT_KERNEL_HEARTBEAT_MIN_INTERVAL_S": "10",
        "AGENT_KERNEL_HEARTBEAT_STALE_CHECK_AGE_S": "30",
        "AGENT_KERNEL_DEFAULT_MODEL_REF": "gpt-4",
        "AGENT_KERNEL_DEFAULT_TENANT_POLICY_REF": "policy:custom",
        "AGENT_KERNEL_DEFAULT_PERMISSION_MODE": "permissive",
        "AGENT_KERNEL_PHASE_TIMEOUT_S": "15.5",
        "AGENT_KERNEL_CIRCUIT_BREAKER_THRESHOLD": "10",
        "AGENT_KERNEL_CIRCUIT_BREAKER_HALF_OPEN_MS": "60000",
        "AGENT_KERNEL_HISTORY_RESET_THRESHOLD": "20000",
    }

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in self._ENV_OVERRIDES.items():
            monkeypatch.setenv(key, value)

    def test_int_overrides(self) -> None:
        cfg = KernelConfig.from_env()
        assert cfg.max_tracked_runs == 500
        assert cfg.max_retained_runs == 250
        assert cfg.max_turn_cache_size == 1000
        assert cfg.http_port == 9000
        assert cfg.max_request_body_bytes == 2_097_152
        assert cfg.heartbeat_dispatching_timeout_s == 60
        assert cfg.heartbeat_waiting_result_timeout_s == 120
        assert cfg.heartbeat_waiting_external_timeout_s == 7200
        assert cfg.heartbeat_waiting_human_timeout_s == 43200
        assert cfg.heartbeat_recovering_timeout_s == 90
        assert cfg.heartbeat_min_interval_s == 10
        assert cfg.heartbeat_stale_check_age_s == 30
        assert cfg.circuit_breaker_threshold == 10
        assert cfg.circuit_breaker_half_open_ms == 60_000
        assert cfg.history_reset_threshold == 20_000

    def test_str_overrides(self) -> None:
        cfg = KernelConfig.from_env()
        assert cfg.api_key == "secret-key-42"
        assert cfg.default_model_ref == "gpt-4"
        assert cfg.default_tenant_policy_ref == "policy:custom"
        assert cfg.default_permission_mode == "permissive"

    def test_float_override(self) -> None:
        cfg = KernelConfig.from_env()
        assert cfg.phase_timeout_s == pytest.approx(15.5)


class TestFromEnvNoVarsReturnsDefaults:
    """Verify from_env() with no env vars returns the same as the default constructor."""

    def test_matches_defaults(self) -> None:
        cfg = KernelConfig.from_env()
        default = KernelConfig()
        assert cfg == default


class TestFrozen:
    """Verify that KernelConfig instances are immutable."""

    def test_assignment_raises(self) -> None:
        cfg = KernelConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.max_tracked_runs = 999  # type: ignore[misc]

    def test_assignment_on_str_field_raises(self) -> None:
        cfg = KernelConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.default_model_ref = "other"  # type: ignore[misc]

    def test_assignment_on_optional_field_raises(self) -> None:
        cfg = KernelConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.phase_timeout_s = 10.0  # type: ignore[misc]
