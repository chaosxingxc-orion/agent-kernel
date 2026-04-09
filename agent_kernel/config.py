"""Centralized kernel configuration.

Collects all tunable constants that were previously scattered across modules
into a single frozen dataclass.  Every field has a sensible default that
matches the value historically hardcoded at the point of use.

Usage::

    from agent_kernel.config import KernelConfig

    # All defaults (matches existing behaviour)
    cfg = KernelConfig()

    # Override selectively
    cfg = KernelConfig(http_port=9000, phase_timeout_s=30.0)

    # Build from environment variables (AGENT_KERNEL_ prefix)
    cfg = KernelConfig.from_env()
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """Centralized kernel configuration.

    All values have sensible defaults that match the previously hardcoded
    constants.  Override via constructor or :meth:`from_env`.
    """

    # -- Facade (kernel_facade.py) -----------------------------------------
    max_tracked_runs: int = 10_000

    # -- Projection / Runtime (minimal_runtime.py) -------------------------
    max_retained_runs: int = 5_000

    # -- Local Gateway (substrate/local/adaptor.py) ------------------------
    max_turn_cache_size: int = 5_000

    # -- HTTP Server (service/http_server.py) ------------------------------
    http_port: int = 8_400
    max_request_body_bytes: int = 1_048_576
    api_key: str | None = None

    # -- Heartbeat (runtime/heartbeat.py) ----------------------------------
    heartbeat_dispatching_timeout_s: int = 300
    heartbeat_waiting_result_timeout_s: int = 600
    heartbeat_waiting_external_timeout_s: int = 3_600
    heartbeat_waiting_human_timeout_s: int = 86_400
    heartbeat_recovering_timeout_s: int = 180
    heartbeat_min_interval_s: int = 5
    heartbeat_stale_check_age_s: int = 60

    # -- Turn Engine (kernel/turn_engine.py) -------------------------------
    default_model_ref: str = "echo"
    default_tenant_policy_ref: str = "policy:default"
    default_permission_mode: str = "strict"
    phase_timeout_s: float | None = None

    # -- Recovery / Circuit Breaker (kernel/recovery/gate.py) --------------
    circuit_breaker_threshold: int = 5
    circuit_breaker_half_open_ms: int = 30_000

    # -- Temporal (substrate/temporal/run_actor_workflow.py) ----------------
    history_reset_threshold: int = 10_000

    @classmethod
    def from_env(cls) -> KernelConfig:
        """Build config from environment variables with ``AGENT_KERNEL_`` prefix.

        Only fields that have a corresponding environment variable set are
        overridden; all other fields keep their defaults.

        Returns:
            A new ``KernelConfig`` instance.

        """
        env_map: dict[str, tuple[str, type]] = {
            "AGENT_KERNEL_MAX_TRACKED_RUNS": ("max_tracked_runs", int),
            "AGENT_KERNEL_MAX_RETAINED_RUNS": ("max_retained_runs", int),
            "AGENT_KERNEL_MAX_TURN_CACHE_SIZE": ("max_turn_cache_size", int),
            "AGENT_KERNEL_HTTP_PORT": ("http_port", int),
            "AGENT_KERNEL_MAX_REQUEST_BODY_BYTES": ("max_request_body_bytes", int),
            "AGENT_KERNEL_API_KEY": ("api_key", str),
            "AGENT_KERNEL_HEARTBEAT_DISPATCHING_TIMEOUT_S": (
                "heartbeat_dispatching_timeout_s",
                int,
            ),
            "AGENT_KERNEL_HEARTBEAT_WAITING_RESULT_TIMEOUT_S": (
                "heartbeat_waiting_result_timeout_s",
                int,
            ),
            "AGENT_KERNEL_HEARTBEAT_WAITING_EXTERNAL_TIMEOUT_S": (
                "heartbeat_waiting_external_timeout_s",
                int,
            ),
            "AGENT_KERNEL_HEARTBEAT_WAITING_HUMAN_TIMEOUT_S": (
                "heartbeat_waiting_human_timeout_s",
                int,
            ),
            "AGENT_KERNEL_HEARTBEAT_RECOVERING_TIMEOUT_S": (
                "heartbeat_recovering_timeout_s",
                int,
            ),
            "AGENT_KERNEL_HEARTBEAT_MIN_INTERVAL_S": ("heartbeat_min_interval_s", int),
            "AGENT_KERNEL_HEARTBEAT_STALE_CHECK_AGE_S": (
                "heartbeat_stale_check_age_s",
                int,
            ),
            "AGENT_KERNEL_DEFAULT_MODEL_REF": ("default_model_ref", str),
            "AGENT_KERNEL_DEFAULT_TENANT_POLICY_REF": (
                "default_tenant_policy_ref",
                str,
            ),
            "AGENT_KERNEL_DEFAULT_PERMISSION_MODE": ("default_permission_mode", str),
            "AGENT_KERNEL_PHASE_TIMEOUT_S": ("phase_timeout_s", float),
            "AGENT_KERNEL_CIRCUIT_BREAKER_THRESHOLD": (
                "circuit_breaker_threshold",
                int,
            ),
            "AGENT_KERNEL_CIRCUIT_BREAKER_HALF_OPEN_MS": (
                "circuit_breaker_half_open_ms",
                int,
            ),
            "AGENT_KERNEL_HISTORY_RESET_THRESHOLD": (
                "history_reset_threshold",
                int,
            ),
        }
        kwargs: dict[str, Any] = {}
        for env_var, (field_name, type_fn) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                kwargs[field_name] = type_fn(val)
        return cls(**kwargs)
