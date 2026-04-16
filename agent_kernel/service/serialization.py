"""DTO <-> JSON serialization for the HTTP service layer.

All kernel DTOs are frozen dataclasses. This module provides thin converters
so the HTTP layer never leaks dataclass internals into JSON wire format.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    BranchStateUpdateRequest,
    CancelRunRequest,
    HumanGateRequest,
    OpenBranchRequest,
    ResumeRunRequest,
    RunPolicyVersions,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
    TaskViewRecord,
)

# ---------------------------------------------------------------------------
# DTO -> JSON (response serialization)
# ---------------------------------------------------------------------------


def serialize_dataclass(obj: Any) -> dict[str, Any]:
    """Convert a frozen dataclass to a JSON-safe dict.

    Handles nested dataclasses, frozensets, and None values.
    """
    if obj is None:
        return {}
    raw = asdict(obj)
    return _normalize(raw)


def _normalize(value: Any) -> Any:
    """Normalizes objects into JSON-serializable values."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


# ---------------------------------------------------------------------------
# JSON -> DTO (request deserialization)
# ---------------------------------------------------------------------------


def deserialize_start_run(data: dict[str, Any]) -> StartRunRequest:
    """Build StartRunRequest from JSON body.

    Accepts either flat policy_version fields or a nested ``policy_versions``
    object (hi-agent style).  Flat fields take precedence.
    """
    # Allow hi-agent to send a nested policy_versions dict.
    pvs = data.get("policy_versions") or {}
    return StartRunRequest(
        initiator=data.get("initiator", "user"),
        run_kind=data.get("run_kind", "default"),
        input_json=data.get("input_json"),
        session_id=data.get("session_id"),
        parent_run_id=data.get("parent_run_id"),
        initial_stage_id=data.get("initial_stage_id"),
        task_contract_ref=data.get("task_contract_ref"),
        route_policy_version=(data.get("route_policy_version") or pvs.get("route_policy_version")),
        skill_policy_version=(data.get("skill_policy_version") or pvs.get("skill_policy_version")),
        evaluation_policy_version=(
            data.get("evaluation_policy_version") or pvs.get("evaluation_policy_version")
        ),
        task_view_policy_version=(
            data.get("task_view_policy_version") or pvs.get("task_view_policy_version")
        ),
    )


def deserialize_signal_run(run_id: str, data: dict[str, Any]) -> SignalRunRequest:
    """Build SignalRunRequest from JSON body."""
    return SignalRunRequest(
        run_id=run_id,
        signal_type=data["signal_type"],
        signal_payload=data.get("signal_payload", {}),
        caused_by=data.get("caused_by"),
    )


def deserialize_cancel_run(run_id: str, data: dict[str, Any]) -> CancelRunRequest:
    """Build CancelRunRequest from JSON body."""
    return CancelRunRequest(
        run_id=run_id,
        reason=data.get("reason", ""),
        caused_by=data.get("caused_by"),
    )


def deserialize_resume_run(run_id: str, data: dict[str, Any]) -> ResumeRunRequest:
    """Build ResumeRunRequest from JSON body."""
    return ResumeRunRequest(
        run_id=run_id,
        caused_by=data.get("caused_by"),
    )


def deserialize_spawn_child_run(
    run_id: str,
    data: dict[str, Any],
) -> SpawnChildRunRequest:
    """Build SpawnChildRunRequest from JSON body."""
    return SpawnChildRunRequest(
        parent_run_id=run_id,
        child_kind=data["child_kind"],
        input_ref=data.get("input_ref"),
        input_json=data.get("input_json"),
        context_ref=data.get("context_ref"),
        task_id=data.get("task_id"),
        inherit_policy_versions=data.get("inherit_policy_versions", True),
        policy_version_overrides=data.get("policy_version_overrides"),
        notify_parent_on_complete=data.get("notify_parent_on_complete", True),
    )


def deserialize_approval(run_id: str, data: dict[str, Any]) -> ApprovalRequest:
    """Build ApprovalRequest from JSON body."""
    return ApprovalRequest(
        run_id=run_id,
        approval_ref=data["approval_ref"],
        approved=data["approved"],
        reviewer_id=data.get("reviewer_id", "anonymous"),
        reason=data.get("reason"),
        caused_by=data.get("caused_by"),
    )


def deserialize_open_branch(run_id: str, data: dict[str, Any]) -> OpenBranchRequest:
    """Build OpenBranchRequest from JSON body."""
    return OpenBranchRequest(
        run_id=run_id,
        branch_id=data["branch_id"],
        stage_id=data["stage_id"],
        parent_branch_id=data.get("parent_branch_id"),
        proposed_by=data.get("proposed_by"),
    )


def deserialize_branch_state_update(
    run_id: str,
    branch_id: str,
    data: dict[str, Any],
) -> BranchStateUpdateRequest:
    """Build BranchStateUpdateRequest from JSON body."""
    return BranchStateUpdateRequest(
        run_id=run_id,
        branch_id=branch_id,
        new_state=data["new_state"],
        failure_code=data.get("failure_code"),
        reason=data.get("reason"),
    )


def deserialize_human_gate(run_id: str, data: dict[str, Any]) -> HumanGateRequest:
    """Build HumanGateRequest from JSON body."""
    return HumanGateRequest(
        gate_ref=data["gate_ref"],
        gate_type=data["gate_type"],
        run_id=run_id,
        trigger_reason=data["trigger_reason"],
        trigger_source=data["trigger_source"],
        stage_id=data.get("stage_id"),
        branch_id=data.get("branch_id"),
        artifact_ref=data.get("artifact_ref"),
        caused_by=data.get("caused_by"),
    )


def deserialize_task_view(run_id: str, data: dict[str, Any]) -> TaskViewRecord:
    """Build TaskViewRecord from JSON body."""
    policy_versions = None
    if data.get("policy_versions"):
        policy_versions = RunPolicyVersions(**data["policy_versions"])
    return TaskViewRecord(
        task_view_id=data["task_view_id"],
        run_id=run_id,
        selected_model_role=data["selected_model_role"],
        assembled_at=data["assembled_at"],
        decision_ref=data.get("decision_ref"),
        stage_id=data.get("stage_id"),
        branch_id=data.get("branch_id"),
        task_contract_ref=data.get("task_contract_ref"),
        evidence_refs=data.get("evidence_refs", []),
        memory_refs=data.get("memory_refs", []),
        knowledge_refs=data.get("knowledge_refs", []),
        policy_versions=policy_versions,
    )
