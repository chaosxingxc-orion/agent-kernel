"""Round 10 — Replay fidelity tests for TurnEngine determinism (criterion 2.2).

Verifies that TurnEngine produces deterministic results across simulated worker
restarts, covering all FSM crash points and both in-memory and SQLite-backed
dedupe stores.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_action(
    action_id: str = "act-1",
    run_id: str = "run-fidelity",
    effect_class: str = "test",
    action_type: str = "tool_call",
) -> Any:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotInput
    from agent_kernel.kernel.contracts import Action

    action = MagicMock(spec=Action)
    action.action_id = action_id
    action.action_type = action_type
    action.effect_class = effect_class
    action.input_json = {}
    action.timeout_ms = None
    action.snapshot_input = CapabilitySnapshotInput(
        run_id=run_id,
        based_on_offset=0,
        tenant_policy_ref="policy:default",
        permission_mode="strict",
    )
    return action


def _make_turn_input(
    run_id: str = "run-fidelity",
    based_on_offset: int = 0,
    through_offset: int = 1,
) -> Any:
    from agent_kernel.kernel.turn_engine import TurnInput

    return TurnInput(
        run_id=run_id,
        through_offset=through_offset,
        based_on_offset=based_on_offset,
        trigger_type="start",
    )


class _AlwaysAdmit:
    async def admit(self, action: Any, snapshot: Any) -> bool:
        return True

    async def check(self, action: Any, snapshot: Any) -> bool:
        return True


class _AlwaysAckExecutor:
    async def execute(
        self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
    ) -> dict:
        return {"acknowledged": True}


class _UnknownEffectExecutor:
    async def execute(
        self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
    ) -> dict:
        return {"acknowledged": False}


class _RaisingExecutor:
    async def execute(
        self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
    ) -> dict:
        raise RuntimeError("simulated executor crash")


def _build_engine(dedupe_store: Any, executor: Any = None) -> Any:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuilder
    from agent_kernel.kernel.turn_engine import TurnEngine

    return TurnEngine(
        snapshot_builder=CapabilitySnapshotBuilder(),
        admission_service=_AlwaysAdmit(),
        dedupe_store=dedupe_store,
        executor=executor or _AlwaysAckExecutor(),
    )


def _pre_populate_dedupe(store: Any, turn_input: Any, action: Any, final_state: str) -> None:
    """Pre-seeds the dedupe store with a specific terminal state."""
    from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope
    from agent_kernel.kernel.turn_engine import _build_turn_identity

    ti = _build_turn_identity(input_value=turn_input, action=action)
    envelope = IdempotencyEnvelope(
        dispatch_idempotency_key=ti.dispatch_dedupe_key,
        operation_fingerprint=ti.decision_fingerprint,
        attempt_seq=1,
        effect_scope="test",
        capability_snapshot_hash="pre-hash",
        host_kind="local_process",
    )
    store.reserve(envelope)
    if final_state in ("dispatched", "acknowledged", "unknown_effect"):
        store.mark_dispatched(ti.dispatch_dedupe_key)
    if final_state == "acknowledged":
        store.mark_acknowledged(ti.dispatch_dedupe_key)
    elif final_state == "unknown_effect":
        store.mark_unknown_effect(ti.dispatch_dedupe_key)


# ---------------------------------------------------------------------------
# TestTurnFidelityRecord
# ---------------------------------------------------------------------------


class TestTurnFidelityRecord:
    def test_frozen_slots_mutation_raises(self) -> None:
        """TurnFidelityRecord is frozen — mutation raises FrozenInstanceError."""
        from agent_kernel.kernel.replay_fidelity import TurnFidelityRecord

        record = TurnFidelityRecord(
            outcome_kind="dispatched",
            snapshot_hash="sha256-abc",
            dedupe_state="acknowledged",
            event_count=3,
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            record.outcome_kind = "blocked"  # type: ignore[misc]

    def test_snapshot_hash_matches_when_equal(self) -> None:
        """snapshot_hash field equality is correctly compared."""
        from agent_kernel.kernel.replay_fidelity import FidelityReport, TurnFidelityRecord

        rec1 = TurnFidelityRecord(
            outcome_kind="dispatched",
            snapshot_hash="sha-x",
            dedupe_state="acknowledged",
            event_count=2,
        )
        rec2 = TurnFidelityRecord(
            outcome_kind="blocked",
            snapshot_hash="sha-x",
            dedupe_state="acknowledged",
            event_count=2,
        )
        report = FidelityReport(run_id="r1", original=rec1, replay=rec2)
        assert report.snapshot_hash_matches is True

    def test_is_idempotent_requires_hash_match_and_same_event_count(self) -> None:
        """is_idempotent is False when event_count differs even if hashes match."""
        from agent_kernel.kernel.replay_fidelity import FidelityReport, TurnFidelityRecord

        rec1 = TurnFidelityRecord(
            outcome_kind="dispatched",
            snapshot_hash="sha-x",
            dedupe_state="acknowledged",
            event_count=2,
        )
        rec2 = TurnFidelityRecord(
            outcome_kind="blocked",
            snapshot_hash="sha-x",
            dedupe_state="acknowledged",
            event_count=5,
        )
        report = FidelityReport(run_id="r1", original=rec1, replay=rec2)
        assert report.snapshot_hash_matches is True
        assert report.is_idempotent is False


# ---------------------------------------------------------------------------
# TestFidelityReport
# ---------------------------------------------------------------------------


class TestFidelityReport:
    def _make_report(
        self,
        hash1: str | None,
        hash2: str | None,
        count1: int = 2,
        count2: int = 2,
    ) -> Any:
        from agent_kernel.kernel.replay_fidelity import FidelityReport, TurnFidelityRecord

        return FidelityReport(
            run_id="r1",
            original=TurnFidelityRecord(
                outcome_kind="dispatched",
                snapshot_hash=hash1,
                dedupe_state="acknowledged",
                event_count=count1,
            ),
            replay=TurnFidelityRecord(
                outcome_kind="blocked",
                snapshot_hash=hash2,
                dedupe_state="acknowledged",
                event_count=count2,
            ),
        )

    def test_snapshot_hash_matches_true_when_equal(self) -> None:
        report = self._make_report("sha-a", "sha-a")
        assert report.snapshot_hash_matches is True

    def test_snapshot_hash_matches_false_when_differ(self) -> None:
        report = self._make_report("sha-a", "sha-b")
        assert report.snapshot_hash_matches is False

    def test_is_idempotent_true_when_hash_and_count_match(self) -> None:
        report = self._make_report("sha-a", "sha-a", count1=2, count2=2)
        assert report.is_idempotent is True

    def test_is_idempotent_false_when_event_count_differs(self) -> None:
        """Replay produced extra events — not idempotent."""
        report = self._make_report("sha-a", "sha-a", count1=2, count2=4)
        assert report.is_idempotent is False

    def test_none_none_hash_matches_true(self) -> None:
        """None == None → snapshot_hash_matches=True."""
        report = self._make_report(None, None, count1=0, count2=0)
        assert report.snapshot_hash_matches is True

    def test_none_none_idempotent_depends_on_event_count(self) -> None:
        report_same = self._make_report(None, None, count1=0, count2=0)
        report_diff = self._make_report(None, None, count1=0, count2=1)
        assert report_same.is_idempotent is True
        assert report_diff.is_idempotent is False

    def test_run_id_stored_correctly(self) -> None:
        from agent_kernel.kernel.replay_fidelity import FidelityReport, TurnFidelityRecord

        rec = TurnFidelityRecord(
            outcome_kind="noop", snapshot_hash=None, dedupe_state=None, event_count=0
        )
        report = FidelityReport(run_id="run-xyz", original=rec, replay=rec)
        assert report.run_id == "run-xyz"


# ---------------------------------------------------------------------------
# TestReplayFidelityVerifier_InMemory
# ---------------------------------------------------------------------------


class TestReplayFidelityVerifierInMemory:
    def test_successful_turn_replay_blocked_is_idempotent(self) -> None:
        """Original dispatches; replay is blocked by dedupe → is_idempotent=True."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action()
        turn_input = _make_turn_input()

        engine = _build_engine(store, _AlwaysAckExecutor())
        replay_engine = _build_engine(store, _AlwaysAckExecutor())
        verifier = ReplayFidelityVerifier()

        report = asyncio.run(
            verifier.verify(
                engine=engine,
                replay_engine=replay_engine,
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.is_idempotent is True
        assert report.original.outcome_kind == "dispatched"
        assert report.replay.outcome_kind == "blocked"

    def test_crash_before_dispatch_reserved_state_replay(self) -> None:
        """Pre-populated 'reserved' state → replay is blocked (duplicate dedupe key)."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        store = InMemoryDedupeStore()
        action = _make_action(run_id="run-reserved")
        turn_input = _make_turn_input(run_id="run-reserved")

        # Simulate crash: pre-populate as "reserved" only.
        _pre_populate_dedupe(store, turn_input, action, "reserved")

        # Replay engine: key already reserved → dispatch blocked.
        replay_engine = _build_engine(store, _AlwaysAckExecutor())
        result = asyncio.run(replay_engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"

    def test_unknown_effect_turn_replay_is_idempotent(self) -> None:
        """Original marks unknown_effect; replay is blocked → is_idempotent=True."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-unk")
        turn_input = _make_turn_input(run_id="run-unk")

        engine = _build_engine(store, _UnknownEffectExecutor())
        replay_engine = _build_engine(store, _UnknownEffectExecutor())
        verifier = ReplayFidelityVerifier()

        report = asyncio.run(
            verifier.verify(
                engine=engine,
                replay_engine=replay_engine,
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.is_idempotent is True

    def test_multiple_actions_produce_independent_hashes(self) -> None:
        """Different actions produce different snapshot_hash values in original records."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store1 = InMemoryDedupeStore()
        store2 = InMemoryDedupeStore()
        event_log1 = InMemoryKernelRuntimeEventLog()
        event_log2 = InMemoryKernelRuntimeEventLog()

        action1 = _make_action(action_id="act-alpha", run_id="run-multi")
        action2 = _make_action(action_id="act-beta", run_id="run-multi")
        turn_input = _make_turn_input(run_id="run-multi")

        verifier = ReplayFidelityVerifier()

        report1 = asyncio.run(
            verifier.verify(
                engine=_build_engine(store1),
                replay_engine=_build_engine(store1),
                turn_input=turn_input,
                action=action1,
                dedupe_store=store1,
                event_log=event_log1,
            )
        )
        report2 = asyncio.run(
            verifier.verify(
                engine=_build_engine(store2),
                replay_engine=_build_engine(store2),
                turn_input=turn_input,
                action=action2,
                dedupe_store=store2,
                event_log=event_log2,
            )
        )

        # Different actions → different snapshot hashes (derived from decision_fingerprint).
        assert report1.original.snapshot_hash != report2.original.snapshot_hash
        assert report1.is_idempotent is True
        assert report2.is_idempotent is True

    def test_replay_engine_with_different_executor_same_snapshot_hash(self) -> None:
        """Replay with a different executor produces same snapshot_hash (deterministic)."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-diff-exec")
        turn_input = _make_turn_input(run_id="run-diff-exec")

        engine = _build_engine(store, _AlwaysAckExecutor())
        # Replay engine uses a different executor but same store.
        replay_engine = _build_engine(store, _UnknownEffectExecutor())
        verifier = ReplayFidelityVerifier()

        report = asyncio.run(
            verifier.verify(
                engine=engine,
                replay_engine=replay_engine,
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        # Replay is blocked by dedupe regardless of executor type.
        assert report.replay.outcome_kind == "blocked"
        assert report.snapshot_hash_matches is True

    def test_replay_outcome_kind_is_blocked(self) -> None:
        """Replay of a dispatched turn always produces outcome_kind='blocked'."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-blk")
        turn_input = _make_turn_input(run_id="run-blk")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.replay.outcome_kind == "blocked"

    def test_event_count_does_not_grow_on_replay(self) -> None:
        """Replay does not append new events to the in-memory event log."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-count")
        turn_input = _make_turn_input(run_id="run-count")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        # Event count after original and after replay should be the same.
        assert report.original.event_count == report.replay.event_count

    def test_snapshot_hash_non_none_for_dispatched_outcome(self) -> None:
        """snapshot_hash is non-None when CapabilitySnapshotBuilder is used and turn dispatches."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-hash-check")
        turn_input = _make_turn_input(run_id="run-hash-check")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        # Both original and replay have non-None snapshot_hash.
        assert report.original.snapshot_hash is not None
        assert report.replay.snapshot_hash is not None

    def test_two_replays_in_sequence_all_idempotent(self) -> None:
        """Running verify twice on different actions both produce idempotent reports."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        event_log = InMemoryKernelRuntimeEventLog()
        verifier = ReplayFidelityVerifier()

        for idx in range(2):
            store = InMemoryDedupeStore()
            action = _make_action(action_id=f"act-seq-{idx}", run_id=f"run-seq-{idx}")
            turn_input = _make_turn_input(run_id=f"run-seq-{idx}")

            report = asyncio.run(
                verifier.verify(
                    engine=_build_engine(store),
                    replay_engine=_build_engine(store),
                    turn_input=turn_input,
                    action=action,
                    dedupe_store=store,
                    event_log=event_log,
                )
            )
            assert report.is_idempotent is True, f"Replay {idx} not idempotent"

    def test_verifier_can_be_reused_across_multiple_verify_calls(self) -> None:
        """ReplayFidelityVerifier is stateless and reusable across calls."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        verifier = ReplayFidelityVerifier()

        for idx in range(3):
            store = InMemoryDedupeStore()
            event_log = InMemoryKernelRuntimeEventLog()
            action = _make_action(action_id=f"act-reuse-{idx}", run_id=f"run-reuse-{idx}")
            turn_input = _make_turn_input(run_id=f"run-reuse-{idx}")

            report = asyncio.run(
                verifier.verify(
                    engine=_build_engine(store),
                    replay_engine=_build_engine(store),
                    turn_input=turn_input,
                    action=action,
                    dedupe_store=store,
                    event_log=event_log,
                )
            )
            assert report.is_idempotent is True


# ---------------------------------------------------------------------------
# TestReplayFidelityVerifier_SQLite
# ---------------------------------------------------------------------------


class TestReplayFidelityVerifierSQLite:
    def test_full_round_trip_sqlite_dedupe_in_memory_event_log(self) -> None:
        """SQLiteDedupeStore in-memory + InMemoryKernelRuntimeEventLog round-trip."""
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = SQLiteDedupeStore(":memory:")
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-sqlite-rt")
        turn_input = _make_turn_input(run_id="run-sqlite-rt")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.is_idempotent is True
        store.close()

    def test_same_snapshot_hash_across_sqlite_backed_runs(self) -> None:
        """SQLite-backed store: original and replay share same snapshot_hash."""
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = SQLiteDedupeStore(":memory:")
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-sqlite-hash")
        turn_input = _make_turn_input(run_id="run-sqlite-hash")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.snapshot_hash_matches is True
        store.close()

    def test_event_count_stable_across_sqlite_restart(self) -> None:
        """Event count does not grow on replay even with SQLite-backed store."""
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = SQLiteDedupeStore(":memory:")
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-sqlite-cnt")
        turn_input = _make_turn_input(run_id="run-sqlite-cnt")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.original.event_count == report.replay.event_count
        store.close()

    def test_wal_mode_preserves_state_write_close_reopen_replay(self, tmp_path: Any) -> None:
        """WAL mode: write state, close, reopen, replay still blocked."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        db_path = str(tmp_path / "wal_replay.db")
        action = _make_action(run_id="run-wal")
        turn_input = _make_turn_input(run_id="run-wal")

        # Write + close.
        store1 = SQLiteDedupeStore(db_path)
        engine1 = _build_engine(store1, _AlwaysAckExecutor())
        asyncio.run(engine1.run_turn(turn_input, action=action))
        store1.close()

        # Reopen + replay.
        store2 = SQLiteDedupeStore(db_path)
        engine2 = _build_engine(store2, _AlwaysAckExecutor())
        result = asyncio.run(engine2.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"
        store2.close()

    def test_parallel_two_engine_same_sqlite_store(self) -> None:
        """Two engines sharing the same SQLite dedupe store: second is blocked."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        store = SQLiteDedupeStore(":memory:")
        action = _make_action(run_id="run-parallel")
        turn_input = _make_turn_input(run_id="run-parallel")

        engine1 = _build_engine(store, _AlwaysAckExecutor())
        engine2 = _build_engine(store, _AlwaysAckExecutor())

        result1 = asyncio.run(engine1.run_turn(turn_input, action=action))
        result2 = asyncio.run(engine2.run_turn(turn_input, action=action))

        assert result1.outcome_kind == "dispatched"
        assert result2.outcome_kind == "blocked"
        store.close()

    def test_crash_at_reserve_sqlite_pre_seed_reserved(self) -> None:
        """Pre-seed 'reserved' in SQLite; replay is blocked (duplicate key)."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        store = SQLiteDedupeStore(":memory:")
        action = _make_action(run_id="run-sqlite-res")
        turn_input = _make_turn_input(run_id="run-sqlite-res")

        _pre_populate_dedupe(store, turn_input, action, "reserved")

        engine = _build_engine(store, _AlwaysAckExecutor())
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"
        store.close()

    def test_colocated_sqlite_bundle_atomic_dispatch_then_replay(self, tmp_path: Any) -> None:
        """ColocatedSQLiteBundle: atomic_dispatch_record then replay produces blocked."""
        from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
        from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import ColocatedSQLiteBundle
        from agent_kernel.kernel.turn_engine import _build_turn_identity

        db_path = str(tmp_path / "colocated.db")
        bundle = ColocatedSQLiteBundle(db_path)
        # Schema is initialized automatically in __init__.

        action = _make_action(run_id="run-colocated")
        turn_input = _make_turn_input(run_id="run-colocated")
        ti = _build_turn_identity(input_value=turn_input, action=action)

        from datetime import UTC, datetime

        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=ti.dispatch_dedupe_key,
            operation_fingerprint=ti.decision_fingerprint,
            attempt_seq=1,
            effect_scope="test",
            capability_snapshot_hash="colocated-hash",
            host_kind="local_process",
        )
        now_iso = datetime.now(tz=UTC).isoformat()
        commit = ActionCommit(
            run_id="run-colocated",
            commit_id="commit-colocated-1",
            created_at=now_iso,
            events=[
                RuntimeEvent(
                    run_id="run-colocated",
                    event_id="evt-1",
                    commit_offset=1,
                    event_type="action_dispatched",
                    event_class="fact",
                    event_authority="authoritative_fact",
                    ordering_key="run-colocated",
                    wake_policy="wake_actor",
                    created_at=now_iso,
                    idempotency_key=ti.dispatch_dedupe_key,
                )
            ],
        )
        bundle.atomic_dispatch_record(commit, envelope)

        # Replay with same key: must be blocked.
        engine = _build_engine(bundle.dedupe_store, _AlwaysAckExecutor())
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"
        bundle.close()

    def test_verify_produces_report_with_correct_run_id(self) -> None:
        """FidelityReport.run_id matches TurnInput.run_id."""
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = SQLiteDedupeStore(":memory:")
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-id-check")
        turn_input = _make_turn_input(run_id="run-id-check")

        verifier = ReplayFidelityVerifier()
        report = asyncio.run(
            verifier.verify(
                engine=_build_engine(store),
                replay_engine=_build_engine(store),
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.run_id == "run-id-check"
        store.close()


# ---------------------------------------------------------------------------
# TestCrashPointMatrix
# ---------------------------------------------------------------------------


class TestCrashPointMatrix:
    """Verifies TurnEngine replay behaviour for each FSM crash point."""

    def test_crash_at_pre_reserve_no_entry_replay_dispatches(self) -> None:
        """No dedupe entry at all: replay dispatches normally → acknowledged."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        store = InMemoryDedupeStore()
        action = _make_action(run_id="run-crash-pre")
        turn_input = _make_turn_input(run_id="run-crash-pre")

        engine = _build_engine(store, _AlwaysAckExecutor())
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "dispatched"

    def test_crash_at_reserved_replay_is_blocked(self) -> None:
        """Dedupe 'reserved': replay blocks (cannot dispatch again)."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        store = InMemoryDedupeStore()
        action = _make_action(run_id="run-crash-reserved")
        turn_input = _make_turn_input(run_id="run-crash-reserved")

        _pre_populate_dedupe(store, turn_input, action, "reserved")

        engine = _build_engine(store, _AlwaysAckExecutor())
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"

    def test_crash_at_dispatched_replay_is_blocked(self) -> None:
        """Dedupe 'dispatched' (executor started, no ack): replay blocks."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        store = InMemoryDedupeStore()
        action = _make_action(run_id="run-crash-disp")
        turn_input = _make_turn_input(run_id="run-crash-disp")

        _pre_populate_dedupe(store, turn_input, action, "dispatched")

        engine = _build_engine(store, _AlwaysAckExecutor())
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"

    def test_crash_at_unknown_effect_replay_is_blocked_idempotent(self) -> None:
        """Dedupe 'unknown_effect': replay blocks → dedupe state unchanged."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier
        from agent_kernel.kernel.turn_engine import _build_turn_identity

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-crash-unk")
        turn_input = _make_turn_input(run_id="run-crash-unk")

        _pre_populate_dedupe(store, turn_input, action, "unknown_effect")

        engine = _build_engine(store, _AlwaysAckExecutor())
        replay_engine = _build_engine(store, _AlwaysAckExecutor())
        verifier = ReplayFidelityVerifier()

        report = asyncio.run(
            verifier.verify(
                engine=engine,
                replay_engine=replay_engine,
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        # Both turns are blocked (key already exists in unknown_effect state).
        assert report.original.outcome_kind == "blocked"
        assert report.replay.outcome_kind == "blocked"
        assert report.is_idempotent is True

        # Dedupe state remains unknown_effect — no state regression.
        ti = _build_turn_identity(input_value=turn_input, action=action)
        record = store.get(ti.dispatch_dedupe_key)
        assert record is not None
        assert record.state == "unknown_effect"

    def test_crash_at_acknowledged_replay_is_blocked_idempotent(self) -> None:
        """Dedupe 'acknowledged': replay blocks; is_idempotent=True."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.kernel.replay_fidelity import ReplayFidelityVerifier

        store = InMemoryDedupeStore()
        event_log = InMemoryKernelRuntimeEventLog()
        action = _make_action(run_id="run-crash-ack")
        turn_input = _make_turn_input(run_id="run-crash-ack")

        _pre_populate_dedupe(store, turn_input, action, "acknowledged")

        engine = _build_engine(store, _AlwaysAckExecutor())
        replay_engine = _build_engine(store, _AlwaysAckExecutor())
        verifier = ReplayFidelityVerifier()

        report = asyncio.run(
            verifier.verify(
                engine=engine,
                replay_engine=replay_engine,
                turn_input=turn_input,
                action=action,
                dedupe_store=store,
                event_log=event_log,
            )
        )
        assert report.original.outcome_kind == "blocked"
        assert report.replay.outcome_kind == "blocked"
        assert report.is_idempotent is True
