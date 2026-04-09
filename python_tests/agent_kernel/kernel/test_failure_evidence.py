"""Tests for deterministic FailureEnvelope evidence-priority behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot
from agent_kernel.kernel.contracts import Action, EffectClass, FailureEnvelope
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.failure_evidence import resolve_failure_evidence
from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput


@dataclass(slots=True)
class _SnapshotBuilder:
    """Builds a deterministic capability snapshot for turn tests."""

    def build(self, *_args: Any, **_kwargs: Any) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            snapshot_ref="snapshot:run-1:1:abc",
            snapshot_hash="hash-1",
            run_id="run-1",
            based_on_offset=1,
            tenant_policy_ref="policy:v1",
            permission_mode="strict",
            tool_bindings=["tool.search"],
            mcp_bindings=[],
            skill_bindings=[],
            feature_flags=[],
            created_at="2026-03-31T00:00:00Z",
        )


@dataclass(slots=True)
class _AdmissionService:
    """Always admits to force TurnEngine into execution path."""

    async def admit(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


@dataclass(slots=True)
class _Executor:
    """Returns caller-provided execution result payload."""

    result: dict[str, Any]

    async def execute(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self.result


def _action() -> Action:
    """Builds one deterministic action for evidence-priority tests."""
    return Action(
        action_id="action-1",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        input_json={"query": "kernel precedence"},
    )


def _turn_input(based_on_offset: int) -> TurnInput:
    """Builds turn input while allowing offset control for dedupe uniqueness."""
    return TurnInput(
        run_id="run-1",
        through_offset=based_on_offset,
        based_on_offset=based_on_offset,
        trigger_type="start",
    )


@pytest.mark.parametrize(
    ("execute_result", "expected_source", "expected_ref"),
    [
        (
            {
                "acknowledged": False,
                "external_ack_ref": "ack-123",
                "evidence_ref": "evidence-123",
                "local_inference": "local-123",
            },
            "external_ack_ref",
            "ack-123",
        ),
        (
            {
                "acknowledged": False,
                "evidence_ref": "evidence-123",
                "local_inference": "local-123",
            },
            "evidence_ref",
            "evidence-123",
        ),
        (
            {"acknowledged": False, "local_inference": "local-123"},
            "local_inference",
            "local-123",
        ),
    ],
)
def test_turn_engine_failure_envelope_uses_evidence_priority_policy(
    execute_result: dict[str, Any],
    expected_source: str,
    expected_ref: str,
) -> None:
    """TurnEngine should stamp deterministic evidence source/ref for recovery."""
    turn_engine = TurnEngine(
        snapshot_builder=_SnapshotBuilder(),
        admission_service=_AdmissionService(),
        dedupe_store=InMemoryDedupeStore(),
        executor=_Executor(result=execute_result),
    )

    result = asyncio.run(turn_engine.run_turn(_turn_input(10), _action()))

    assert result.recovery_input is not None
    assert result.recovery_input.evidence_priority_source == expected_source
    assert result.recovery_input.evidence_priority_ref == expected_ref


def test_failure_evidence_resolution_is_deterministic_for_same_envelope() -> None:
    """Resolver should return identical output across repeated evaluation."""
    envelope = FailureEnvelope(
        run_id="run-1",
        action_id="action-1",
        failed_stage="execution",
        failed_component="executor",
        failure_code="effect_unknown",
        failure_class="unknown",
        external_ack_ref="ack-42",
        evidence_ref="evidence-42",
        local_inference="local-42",
    )

    first = resolve_failure_evidence(envelope)
    second = resolve_failure_evidence(envelope)
    third = resolve_failure_evidence(envelope)

    assert first == second == third
    assert first.source == "external_ack_ref"
    assert first.evidence_ref == "ack-42"
