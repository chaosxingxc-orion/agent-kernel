"""Large stability matrix for dispatch admission behavior invariants."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.kernel.contracts import Action, EffectClass, RunProjection
from agent_kernel.kernel.minimal_runtime import StaticDispatchAdmissionService

_CASE_COUNT = 1000


def _projection_for(seed: int) -> RunProjection:
    ready = seed % 4 in (0, 1)
    waiting_external = seed % 11 == 0
    return RunProjection(
        run_id=f"run-{seed}",
        lifecycle_state="ready" if ready else "created",
        projected_offset=seed,
        waiting_external=waiting_external,
        ready_for_dispatch=ready,
    )


def _action_for(seed: int) -> Action:
    policy_tags: list[str] = []
    timeout_ms: int | None = None
    input_json: dict[str, object] | None = {"estimated_cost": float(seed % 9)}
    external_level: str | None = None
    effect_class = EffectClass.READ_ONLY

    if seed % 13 == 0:
        policy_tags.append("requires_human_review")
    if seed % 17 == 0:
        timeout_ms = 300001
    if seed % 19 == 0:
        policy_tags.append("max_cost:2")
    if seed % 23 == 0:
        effect_class = EffectClass.IDEMPOTENT_WRITE
        external_level = "guaranteed"
        input_json = {"remote_service": {"idempotency_contract": {"returns_stable_ack": False}}}

    return Action(
        action_id=f"action-{seed}",
        run_id=f"run-{seed}",
        action_type="tool.search",
        effect_class=effect_class,  # type: ignore[arg-type]
        external_idempotency_level=external_level,  # type: ignore[arg-type]
        input_json=input_json,  # type: ignore[arg-type]
        policy_tags=policy_tags,
        timeout_ms=timeout_ms,
    )


@pytest.mark.parametrize("seed", list(range(_CASE_COUNT)))
def test_admission_matrix_invariants(seed: int) -> None:
    """Admission must always return structurally valid, stable decision shapes."""
    admission = StaticDispatchAdmissionService()
    action = _action_for(seed)
    projection = _projection_for(seed)

    result = asyncio.run(admission.check(action, projection))

    assert result.reason_code in {
        "ok",
        "permission_denied",
        "quota_exceeded",
        "policy_denied",
        "dependency_not_ready",
        "stale_policy",
        "idempotency_contract_insufficient",
    }
    if result.admitted:
        assert result.reason_code == "ok"
        assert result.grant_ref is not None
        assert result.sandbox_grant is not None
        assert result.idempotency_envelope is not None
    else:
        assert result.reason_code != "ok"
