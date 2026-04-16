"""Large stability matrix for TurnEngine outcome invariants."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuildError,
)
from agent_kernel.kernel.contracts import Action, EffectClass
from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope, InMemoryDedupeStore
from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

_CASE_COUNT = 1000


@dataclass(slots=True)
class _SnapshotBuilder:
    """Test suite for  SnapshotBuilder."""

    should_fail: bool

    def build(self, *_args: Any, **_kwargs: Any) -> CapabilitySnapshot:
        """Builds a test fixture value."""
        if self.should_fail:
            raise CapabilitySnapshotBuildError("matrix fail")
        return CapabilitySnapshot(
            snapshot_ref="snapshot:matrix:1",
            snapshot_hash="hash:matrix",
            run_id="run-matrix",
            based_on_offset=1,
            tenant_policy_ref="policy:matrix",
            permission_mode="strict",
            tool_bindings=[],
            mcp_bindings=[],
            skill_bindings=[],
            feature_flags=[],
            created_at="2026-04-01T00:00:00Z",
        )


@dataclass(slots=True)
class _Admission:
    """Test suite for  Admission."""

    admitted: bool

    async def admit(self, *_args: Any, **_kwargs: Any) -> bool:
        """Admit."""
        return self.admitted


@dataclass(slots=True)
class _Executor:
    """Test suite for  Executor."""

    acknowledged: bool

    async def execute(
        self,
        _action: Action,
        _snapshot: CapabilitySnapshot,
        _envelope: IdempotencyEnvelope,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Executes the test operation."""
        return {"acknowledged": self.acknowledged}


def _action(seed: int) -> Action:
    """Builds an action fixture."""
    return Action(
        action_id=f"a-{seed}",
        run_id="run-matrix",
        action_type="tool.search",
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        input_json={"query": f"q-{seed}"},
    )


def _turn_input(seed: int) -> TurnInput:
    """Turn input."""
    return TurnInput(
        run_id="run-matrix",
        through_offset=seed + 1,
        based_on_offset=seed + 1,
        trigger_type="signal",
    )


@pytest.mark.parametrize("seed", list(range(_CASE_COUNT)))
def test_turn_engine_matrix_outcome_invariants(seed: int) -> None:
    """TurnEngine should produce stable outcome class for each condition bucket."""
    snapshot_fail = seed % 10 == 0
    admission_deny = seed % 10 in (1, 2)
    acknowledged = seed % 2 == 0

    engine = TurnEngine(
        snapshot_builder=_SnapshotBuilder(should_fail=snapshot_fail),
        admission_service=_Admission(admitted=not admission_deny),
        dedupe_store=InMemoryDedupeStore(),
        executor=_Executor(acknowledged=acknowledged),
    )
    result = asyncio.run(engine.run_turn(_turn_input(seed), _action(seed)))

    if snapshot_fail:
        assert result.outcome_kind == "noop"
        assert result.state == "completed_noop"
    elif admission_deny:
        assert result.outcome_kind == "blocked"
        assert result.state == "dispatch_blocked"
    elif acknowledged:
        assert result.outcome_kind == "dispatched"
        assert result.state == "dispatch_acknowledged"
    else:
        assert result.outcome_kind == "recovery_pending"
        assert result.state == "recovery_pending"
