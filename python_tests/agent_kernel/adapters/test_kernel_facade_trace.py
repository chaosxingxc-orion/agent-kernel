"""Tests for KernelFacade TRACE alignment methods (Gap A-I)."""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    BranchStateUpdateRequest,
    HumanGateRequest,
    OpenBranchRequest,
    RunPolicyVersions,
    RunProjection,
    TaskViewRecord,
    TraceFailureCode,
    TraceRuntimeView,
)
from agent_kernel.kernel.dedupe_store import (
    IdempotencyEnvelope,
    InMemoryDedupeStore,
)
from agent_kernel.kernel.persistence.sqlite_task_view_log import SQLiteTaskViewLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway(lifecycle_state: str = "running", waiting_external: bool = False) -> MagicMock:
    gw = MagicMock()
    gw.query_projection = AsyncMock(
        return_value=RunProjection(
            run_id="run-1",
            lifecycle_state=lifecycle_state,  # type: ignore[arg-type]
            projected_offset=0,
            waiting_external=waiting_external,
            ready_for_dispatch=True,
        )
    )
    gw.signal_run = AsyncMock()
    return gw


def _make_facade(**kwargs) -> KernelFacade:
    gw = kwargs.pop("gateway", _make_gateway())
    return KernelFacade(workflow_gateway=gw, **kwargs)


def _task_view(task_view_id: str = "tv-1", run_id: str = "run-1") -> TaskViewRecord:
    return TaskViewRecord(
        task_view_id=task_view_id,
        run_id=run_id,
        decision_ref="decision-ref-1",
        selected_model_role="heavy_reasoning",
        assembled_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# query_trace_runtime
# ---------------------------------------------------------------------------


class TestQueryTraceRuntime:
    def test_running_run_maps_to_active(self):
        gw = _make_gateway(lifecycle_state="running")
        facade = _make_facade(gateway=gw)
        result = asyncio.run(facade.query_trace_runtime("run-1"))
        assert isinstance(result, TraceRuntimeView)
        assert result.run_id == "run-1"
        assert result.run_state == "active"
        assert result.wait_state == "none"
        assert result.review_state == "not_required"
        assert result.branches == []

    def test_completed_run_maps_to_completed(self):
        gw = _make_gateway(lifecycle_state="completed")
        facade = _make_facade(gateway=gw)
        result = asyncio.run(facade.query_trace_runtime("run-1"))
        assert result.run_state == "completed"

    def test_waiting_external_maps_wait_state(self):
        gw = _make_gateway(lifecycle_state="waiting_callback", waiting_external=True)
        facade = _make_facade(gateway=gw)
        result = asyncio.run(facade.query_trace_runtime("run-1"))
        assert result.wait_state == "external_callback"

    def test_branches_included_after_open_branch(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        req = OpenBranchRequest(
            run_id="run-1",
            branch_id="branch-1",
            stage_id="stage-A",
        )
        asyncio.run(facade.open_branch(req))
        result = asyncio.run(facade.query_trace_runtime("run-1"))
        assert len(result.branches) == 1
        assert result.branches[0].branch_id == "branch-1"
        assert result.branches[0].state == "active"


# ---------------------------------------------------------------------------
# record_task_view / get_task_view_record / get_task_view_by_decision
# ---------------------------------------------------------------------------


class TestTaskViewLog:
    def test_record_and_retrieve_by_id(self):
        log = SQLiteTaskViewLog()
        facade = _make_facade(task_view_log=log)
        record = _task_view()
        facade.record_task_view(record)
        retrieved = facade.get_task_view_record("tv-1")
        assert retrieved is not None
        assert retrieved.task_view_id == "tv-1"
        assert retrieved.run_id == "run-1"

    def test_record_and_retrieve_by_decision(self):
        log = SQLiteTaskViewLog()
        facade = _make_facade(task_view_log=log)
        record = _task_view()
        facade.record_task_view(record)
        retrieved = facade.get_task_view_by_decision("run-1", "decision-ref-1")
        assert retrieved is not None
        assert retrieved.task_view_id == "tv-1"

    def test_get_missing_returns_none(self):
        log = SQLiteTaskViewLog()
        facade = _make_facade(task_view_log=log)
        assert facade.get_task_view_record("nonexistent") is None

    def test_no_task_view_log_raises(self):
        facade = _make_facade()
        with pytest.raises(RuntimeError, match="task_view_log"):
            facade.record_task_view(_task_view())

    def test_record_with_policy_versions(self):
        log = SQLiteTaskViewLog()
        facade = _make_facade(task_view_log=log)
        pv = RunPolicyVersions(
            route_policy_version="v1",
            skill_policy_version="v2",
            evaluation_policy_version="v3",
            task_view_policy_version="v4",
            pinned_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        record = TaskViewRecord(
            task_view_id="tv-2",
            run_id="run-2",
            decision_ref="dec-2",
            selected_model_role="light_processing",
            assembled_at=datetime.datetime.now(datetime.UTC).isoformat(),
            policy_versions=pv,
            evidence_refs=["ref-a", "ref-b"],
        )
        facade.record_task_view(record)
        retrieved = facade.get_task_view_record("tv-2")
        assert retrieved is not None
        assert retrieved.policy_versions is not None
        assert retrieved.policy_versions.route_policy_version == "v1"
        assert retrieved.evidence_refs == ["ref-a", "ref-b"]

    def test_idempotent_write(self):
        log = SQLiteTaskViewLog()
        facade = _make_facade(task_view_log=log)
        record = _task_view()
        facade.record_task_view(record)
        facade.record_task_view(record)  # second write should be no-op
        retrieved = facade.get_task_view_record("tv-1")
        assert retrieved is not None


# ---------------------------------------------------------------------------
# open_branch / mark_branch_state
# ---------------------------------------------------------------------------


class TestBranchManagement:
    def test_open_branch_sends_signal(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        req = OpenBranchRequest(
            run_id="run-1",
            branch_id="branch-1",
            stage_id="stage-A",
            proposed_by="model",
        )
        asyncio.run(facade.open_branch(req))
        gw.signal_run.assert_called_once()
        call_args = gw.signal_run.call_args[0][0]
        assert call_args.signal_type == "branch_opened"
        assert call_args.signal_payload["branch_id"] == "branch-1"

    def test_open_branch_registers_in_memory(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        req = OpenBranchRequest(run_id="run-1", branch_id="b1", stage_id="s1")
        asyncio.run(facade.open_branch(req))
        with facade._branch_lock:
            assert "b1" in facade._branch_registry.get("run-1", {})

    def test_mark_branch_state_updates_state(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        asyncio.run(
            facade.open_branch(OpenBranchRequest(run_id="run-1", branch_id="b1", stage_id="s1"))
        )
        asyncio.run(
            facade.mark_branch_state(
                BranchStateUpdateRequest(
                    run_id="run-1",
                    branch_id="b1",
                    new_state="pruned",  # type: ignore[arg-type]
                )
            )
        )
        with facade._branch_lock:
            branch = facade._branch_registry["run-1"]["b1"]
        assert branch.state == "pruned"

    def test_mark_branch_state_with_failure_code(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        asyncio.run(
            facade.open_branch(OpenBranchRequest(run_id="run-1", branch_id="b1", stage_id="s1"))
        )
        asyncio.run(
            facade.mark_branch_state(
                BranchStateUpdateRequest(
                    run_id="run-1",
                    branch_id="b1",
                    new_state="failed",  # type: ignore[arg-type]
                    failure_code=TraceFailureCode.CALLBACK_TIMEOUT,
                    reason="timed out",
                )
            )
        )
        signal_calls = gw.signal_run.call_args_list
        last_signal = signal_calls[-1][0][0]
        assert last_signal.signal_type == "branch_state_updated"
        assert last_signal.signal_payload["failure_code"] == "callback_timeout"

    def test_mark_branch_state_unknown_branch_raises(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        with pytest.raises(KeyError, match="nonexistent"):
            asyncio.run(
                facade.mark_branch_state(
                    BranchStateUpdateRequest(
                        run_id="run-1",
                        branch_id="nonexistent",
                        new_state="pruned",  # type: ignore[arg-type]
                    )
                )
            )

    def test_multiple_branches_per_run(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        for i in range(3):
            asyncio.run(
                facade.open_branch(
                    OpenBranchRequest(run_id="run-1", branch_id=f"b{i}", stage_id="s1")
                )
            )
        result = asyncio.run(facade.query_trace_runtime("run-1"))
        assert len(result.branches) == 3


# ---------------------------------------------------------------------------
# open_human_gate
# ---------------------------------------------------------------------------


class TestOpenHumanGate:
    def test_sends_human_gate_signal(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        req = HumanGateRequest(
            gate_ref="gate-1",
            gate_type="final_approval",
            run_id="run-1",
            trigger_reason="irreversible action pending",
            trigger_source="system",
            branch_id="b1",
        )
        asyncio.run(facade.open_human_gate(req))
        gw.signal_run.assert_called_once()
        signal = gw.signal_run.call_args[0][0]
        assert signal.signal_type == "human_gate_opened"
        assert signal.signal_payload["gate_ref"] == "gate-1"
        assert signal.signal_payload["gate_type"] == "final_approval"
        assert signal.signal_payload["trigger_source"] == "system"

    def test_all_gate_types_accepted(self):
        gw = _make_gateway()
        facade = _make_facade(gateway=gw)
        for gate_type in (
            "contract_correction",
            "route_direction",
            "artifact_review",
            "final_approval",
        ):
            gw.signal_run.reset_mock()
            req = HumanGateRequest(
                gate_ref=f"gate-{gate_type}",
                gate_type=gate_type,  # type: ignore[arg-type]
                run_id="run-1",
                trigger_reason="test",
                trigger_source="system",
            )
            asyncio.run(facade.open_human_gate(req))
            gw.signal_run.assert_called_once()


# ---------------------------------------------------------------------------
# get_action_state
# ---------------------------------------------------------------------------


class TestGetActionState:
    def test_raises_when_no_dedupe_store(self):
        facade = _make_facade()
        with pytest.raises(RuntimeError, match="no dedupe_store was injected"):
            facade.get_action_state("any-key")

    def test_returns_none_for_unknown_key(self):
        store = InMemoryDedupeStore()
        facade = _make_facade(dedupe_store=store)
        assert facade.get_action_state("unknown-key") is None

    def test_returns_state_for_known_key(self):
        store = InMemoryDedupeStore()
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key="key-1",
            operation_fingerprint="fp-1",
            attempt_seq=1,
            effect_scope="test",
            capability_snapshot_hash="hash-1",
            host_kind="tool_executor",
        )
        store.reserve(envelope)
        facade = _make_facade(dedupe_store=store)
        assert facade.get_action_state("key-1") == "reserved"

    def test_tracks_state_transitions(self):
        store = InMemoryDedupeStore()
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key="key-2",
            operation_fingerprint="fp-2",
            attempt_seq=1,
            effect_scope="test",
            capability_snapshot_hash="hash-2",
            host_kind="tool_executor",
        )
        store.reserve(envelope)
        store.mark_dispatched("key-2")
        facade = _make_facade(dedupe_store=store)
        assert facade.get_action_state("key-2") == "dispatched"
        store.mark_acknowledged("key-2")
        assert facade.get_action_state("key-2") == "acknowledged"
        store.mark_succeeded("key-2")
        assert facade.get_action_state("key-2") == "succeeded"
