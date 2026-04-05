"""Tests for agent-kernel event export infrastructure.

Covers:
- EventExportingEventLog: fire-and-forget semantics, timeout isolation,
  exception isolation, transparent load() delegation.
- InMemoryRunTraceStore: commit accumulation, TurnTrace derivation,
  lifecycle tracking, terminal detection, failure counting, query helpers.
- KernelRuntimeConfig: event_export_port wiring in _build_services().
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from agent_kernel.kernel.contracts import Action, ActionCommit, RuntimeEvent
from agent_kernel.kernel.event_export import (
    EventExportingEventLog,
    InMemoryRunTraceStore,
)
from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
from agent_kernel.kernel.persistence.event_schema_migration import EventSchemaMigrator

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _make_event(
    run_id: str,
    offset: int,
    event_type: str,
    event_authority: str = "authoritative_fact",
    payload_json: dict[str, Any] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        run_id=run_id,
        event_id=f"evt-{offset}",
        commit_offset=offset,
        event_type=event_type,
        event_class="fact",
        event_authority=event_authority,
        ordering_key=run_id,
        wake_policy="wake_actor",
        created_at=_now(),
        payload_json=payload_json,
    )


def _make_action(run_id: str, action_type: str = "tool_call") -> Action:
    return Action(
        action_id=f"act-{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        action_type=action_type,
        effect_class="idempotent_write",
    )


def _make_commit(
    run_id: str,
    *,
    offset: int = 1,
    action: Action | None = None,
    event_types: list[str] | None = None,
) -> ActionCommit:
    if event_types is None:
        event_types = ["run.started"]
    events = [_make_event(run_id, offset + i, et) for i, et in enumerate(event_types)]
    return ActionCommit(
        run_id=run_id,
        commit_id=f"c-{uuid.uuid4().hex[:8]}",
        events=events,
        created_at=_now(),
        action=action,
    )


# ---------------------------------------------------------------------------
# EventExportingEventLog
# ---------------------------------------------------------------------------


class TestEventExportingEventLog:
    """EventExportingEventLog wraps inner log and exports fire-and-forget."""

    def test_append_returns_commit_ref_from_inner(self) -> None:
        """Commit ref must come from the inner log, not the wrapper."""
        inner = InMemoryKernelRuntimeEventLog()
        export_port = AsyncMock()
        wrapper = EventExportingEventLog(inner, export_port)

        commit = _make_commit("run-1")
        ref = asyncio.run(wrapper.append_action_commit(commit))

        assert isinstance(ref, str)
        assert ref  # non-empty

    def test_export_is_called_after_successful_append(self) -> None:
        """export_commit must be invoked for every successful append."""
        inner = InMemoryKernelRuntimeEventLog()
        export_port = AsyncMock()
        wrapper = EventExportingEventLog(inner, export_port)

        commit = _make_commit("run-2")

        async def _run() -> None:
            await wrapper.append_action_commit(commit)
            # Yield control so the background task runs
            await asyncio.sleep(0)

        asyncio.run(_run())

        export_port.export_commit.assert_awaited_once_with(commit)

    def test_export_failure_does_not_propagate(self) -> None:
        """Exception in export must not raise in caller."""
        inner = InMemoryKernelRuntimeEventLog()

        async def _failing_export(commit: ActionCommit) -> None:
            raise RuntimeError("downstream outage")

        export_port = AsyncMock()
        export_port.export_commit = _failing_export
        wrapper = EventExportingEventLog(inner, export_port)

        async def _run() -> None:
            await wrapper.append_action_commit(_make_commit("run-3"))
            await asyncio.sleep(0)  # let background task run

        asyncio.run(_run())  # must not raise

    def test_export_timeout_does_not_block_execution(self) -> None:
        """Export timeout must be isolated; execution path is unaffected."""
        inner = InMemoryKernelRuntimeEventLog()

        async def _slow_export(commit: ActionCommit) -> None:
            await asyncio.sleep(10)  # far exceeds timeout

        export_port = AsyncMock()
        export_port.export_commit = _slow_export
        wrapper = EventExportingEventLog(inner, export_port, export_timeout_ms=1)  # 1 ms timeout

        async def _run() -> None:
            await wrapper.append_action_commit(_make_commit("run-4"))
            await asyncio.sleep(0.05)  # let timeout fire

        asyncio.run(_run())  # must complete without hanging

    def test_load_delegates_to_inner_log(self) -> None:
        """load() must return exactly what the inner log contains."""
        inner = InMemoryKernelRuntimeEventLog()
        wrapper = EventExportingEventLog(inner, AsyncMock())

        commit = _make_commit("run-5", offset=1, event_types=["run.started", "run.ready"])
        asyncio.run(wrapper.append_action_commit(commit))
        events = asyncio.run(wrapper.load("run-5"))

        assert len(events) == 2
        assert events[0].event_type == "run.started"
        assert events[1].event_type == "run.ready"

    def test_inner_log_receives_all_appends(self) -> None:
        """Inner log must contain all events appended via wrapper."""
        inner = InMemoryKernelRuntimeEventLog()
        wrapper = EventExportingEventLog(inner, AsyncMock())

        for i in range(3):
            asyncio.run(
                wrapper.append_action_commit(
                    _make_commit("run-6", offset=i + 1, event_types=["run.ready"])
                )
            )

        events = asyncio.run(inner.load("run-6"))
        assert len(events) == 3

    def test_export_applies_schema_migration_before_export(self) -> None:
        """Export path should migrate events when migrator + target are set."""
        inner = InMemoryKernelRuntimeEventLog()
        export_port = AsyncMock()
        migrator = EventSchemaMigrator()

        def _migrate(event: RuntimeEvent) -> RuntimeEvent:
            return replace(
                event,
                payload_json={**(event.payload_json or {}), "migrated": True},
            )

        migrator.register(
            "1",
            "2",
            _migrate,
        )
        wrapper = EventExportingEventLog(
            inner,
            export_port,
            event_schema_migrator=migrator,
            target_event_schema_version="2",
        )
        commit = _make_commit("run-migrate", event_types=["run.started"])

        async def _run() -> None:
            await wrapper.append_action_commit(commit)
            await asyncio.sleep(0)

        asyncio.run(_run())
        exported_commit = export_port.export_commit.await_args.args[0]
        assert exported_commit.events[0].schema_version == "2"
        assert exported_commit.events[0].payload_json is not None
        assert exported_commit.events[0].payload_json["migrated"] is True

    def test_close_cancels_pending_exports_and_awaits_inner_close(self) -> None:
        """close() should cancel background tasks and await inner async close."""

        class _Inner:
            def __init__(self) -> None:
                self.closed = False

            async def append_action_commit(self, _commit: ActionCommit) -> str:
                return "commit-ref-1"

            async def load(self, _run_id: str, after_offset: int = 0) -> list[RuntimeEvent]:
                del after_offset
                return []

            async def close(self) -> None:
                self.closed = True

        async def _slow_export(_commit: ActionCommit) -> None:
            await asyncio.sleep(10)

        inner = _Inner()
        export_port = AsyncMock()
        export_port.export_commit = _slow_export
        wrapper = EventExportingEventLog(inner, export_port)
        commit = _make_commit("run-close")

        async def _run() -> None:
            await wrapper.append_action_commit(commit)
            await asyncio.sleep(0)
            await wrapper.close()

        asyncio.run(_run())
        assert inner.closed is True
        assert wrapper._background_tasks == set()


# ---------------------------------------------------------------------------
# InMemoryRunTraceStore
# ---------------------------------------------------------------------------


class TestInMemoryRunTraceStore:
    """InMemoryRunTraceStore accumulates commits into RunTrace per run."""

    def test_get_returns_none_for_unknown_run(self) -> None:
        store = InMemoryRunTraceStore()
        assert store.get("unknown") is None

    def test_first_commit_creates_trace(self) -> None:
        store = InMemoryRunTraceStore()
        commit = _make_commit("run-a", event_types=["run.created", "run.started"])
        asyncio.run(store.export_commit(commit))

        trace = store.get("run-a")
        assert trace is not None
        assert trace.run_id == "run-a"
        assert "run.created" in trace.lifecycle_event_types
        assert "run.started" in trace.lifecycle_event_types

    def test_lifecycle_events_deduplicated_in_order(self) -> None:
        """Repeated lifecycle events should not duplicate in the trace."""
        store = InMemoryRunTraceStore()
        asyncio.run(
            store.export_commit(_make_commit("run-b", offset=1, event_types=["run.started"]))
        )
        asyncio.run(
            store.export_commit(
                _make_commit("run-b", offset=2, event_types=["run.started", "run.ready"])
            )
        )
        trace = store.get("run-b")
        assert trace is not None
        assert trace.lifecycle_event_types.count("run.started") == 1
        assert "run.ready" in trace.lifecycle_event_types

    def test_turn_trace_created_for_action_commit(self) -> None:
        """Commits with an action must produce a TurnTrace."""
        store = InMemoryRunTraceStore()
        action = _make_action("run-c", action_type="file_write")
        commit = _make_commit(
            "run-c",
            action=action,
            event_types=["turn.dispatched", "turn.dispatch_acknowledged", "run.dispatching"],
        )
        asyncio.run(store.export_commit(commit))

        trace = store.get("run-c")
        assert trace is not None
        assert len(trace.turns) == 1
        turn = trace.turns[0]
        assert turn.action_id == action.action_id
        assert turn.action_type == "file_write"
        assert turn.effect_class == "idempotent_write"
        assert turn.outcome_kind == "dispatched"

    def test_noop_turn_outcome(self) -> None:
        store = InMemoryRunTraceStore()
        action = _make_action("run-d")
        asyncio.run(
            store.export_commit(
                _make_commit("run-d", action=action, event_types=["turn.completed_noop"])
            )
        )
        trace = store.get("run-d")
        assert trace is not None
        assert trace.turns[0].outcome_kind == "noop"
        assert trace.failure_count == 0

    def test_blocked_turn_outcome(self) -> None:
        store = InMemoryRunTraceStore()
        action = _make_action("run-e")
        asyncio.run(
            store.export_commit(
                _make_commit("run-e", action=action, event_types=["turn.dispatch_blocked"])
            )
        )
        assert store.get("run-e").turns[0].outcome_kind == "blocked"

    def test_recovery_pending_increments_failure_count(self) -> None:
        store = InMemoryRunTraceStore()
        action = _make_action("run-f")
        asyncio.run(
            store.export_commit(
                _make_commit(
                    "run-f",
                    action=action,
                    event_types=["turn.effect_unknown", "turn.recovery_pending"],
                )
            )
        )
        trace = store.get("run-f")
        assert trace is not None
        assert trace.turns[0].outcome_kind == "recovery_pending"
        assert trace.failure_count == 1

    def test_recovery_mode_from_plan_selected_payload(self) -> None:
        """recovery.plan_selected payload is the preferred source for recovery mode."""
        store = InMemoryRunTraceStore()
        run_id = "run-g"
        action = _make_action(run_id)

        plan_event = RuntimeEvent(
            run_id=run_id,
            event_id="evt-plan",
            commit_offset=1,
            event_type="recovery.plan_selected",
            event_class="derived",
            event_authority="derived_diagnostic",
            ordering_key=run_id,
            wake_policy="projection_only",
            created_at=_now(),
            payload_json={"planned_mode": "human_escalation", "reason": "needs_operator"},
        )
        commit = ActionCommit(
            run_id=run_id,
            commit_id="c-g",
            events=[
                plan_event,
                _make_event(run_id, 2, "run.waiting_external"),
            ],
            created_at=_now(),
            action=action,
        )
        asyncio.run(store.export_commit(commit))

        turn = store.get(run_id).turns[0]
        assert turn.recovery_mode == "human_escalation"

    def test_terminal_detection_on_completed(self) -> None:
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-h", event_types=["run.completed"])))
        trace = store.get("run-h")
        assert trace is not None
        assert trace.is_terminal
        assert trace.terminal_state == "completed"

    def test_terminal_detection_on_aborted(self) -> None:
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-i", event_types=["run.aborted"])))
        trace = store.get("run-i")
        assert trace is not None
        assert trace.is_terminal
        assert trace.terminal_state == "aborted"

    def test_non_terminal_run_not_marked_terminal(self) -> None:
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-j", event_types=["run.ready"])))
        assert not store.get("run-j").is_terminal

    def test_all_returns_sorted_by_run_id(self) -> None:
        store = InMemoryRunTraceStore()
        for run_id in ("run-z", "run-a", "run-m"):
            asyncio.run(store.export_commit(_make_commit(run_id)))
        traces = store.all()
        assert [t.run_id for t in traces] == ["run-a", "run-m", "run-z"]

    def test_terminal_runs_filter(self) -> None:
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-done", event_types=["run.completed"])))
        asyncio.run(store.export_commit(_make_commit("run-alive", event_types=["run.ready"])))
        terminal = store.terminal_runs()
        assert len(terminal) == 1
        assert terminal[0].run_id == "run-done"

    def test_failed_runs_filter(self) -> None:
        store = InMemoryRunTraceStore()
        action_ok = _make_action("run-ok")
        action_fail = _make_action("run-fail")
        asyncio.run(
            store.export_commit(
                _make_commit("run-ok", action=action_ok, event_types=["turn.dispatch_acknowledged"])
            )
        )
        asyncio.run(
            store.export_commit(
                _make_commit(
                    "run-fail",
                    action=action_fail,
                    event_types=["turn.effect_unknown", "turn.recovery_pending"],
                )
            )
        )
        failed = store.failed_runs()
        assert len(failed) == 1
        assert failed[0].run_id == "run-fail"

    def test_multiple_runs_isolated(self) -> None:
        """Commits from different runs must not cross-contaminate traces."""
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-x", event_types=["run.started"])))
        asyncio.run(store.export_commit(_make_commit("run-y", event_types=["run.completed"])))

        assert not store.get("run-x").is_terminal
        assert store.get("run-y").is_terminal

    def test_first_and_last_commit_timestamps_recorded(self) -> None:
        store = InMemoryRunTraceStore()
        asyncio.run(store.export_commit(_make_commit("run-ts", event_types=["run.started"])))
        asyncio.run(
            store.export_commit(_make_commit("run-ts", offset=2, event_types=["run.ready"]))
        )
        trace = store.get("run-ts")
        assert trace is not None
        assert trace.first_commit_at is not None
        assert trace.last_commit_at is not None


# ---------------------------------------------------------------------------
# KernelRuntimeConfig export wiring
# ---------------------------------------------------------------------------


class TestKernelRuntimeConfigExportWiring:
    """_build_services() should wrap event_log when export_port is provided."""

    def test_build_services_without_export_returns_plain_event_log(self) -> None:
        from agent_kernel.runtime.kernel_runtime import KernelRuntimeConfig, _build_services

        config = KernelRuntimeConfig()
        event_log, *_ = _build_services(config)
        assert not isinstance(event_log, EventExportingEventLog)

    def test_build_services_with_export_wraps_event_log(self) -> None:
        from agent_kernel.runtime.kernel_runtime import KernelRuntimeConfig, _build_services

        store = InMemoryRunTraceStore()
        config = KernelRuntimeConfig(event_export_port=store)
        event_log, *_ = _build_services(config)
        assert isinstance(event_log, EventExportingEventLog)

    def test_build_services_projection_reads_from_inner_not_wrapper(self) -> None:
        """Projection service must always read from the raw storage layer."""
        from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
        from agent_kernel.runtime.kernel_runtime import KernelRuntimeConfig, _build_services

        store = InMemoryRunTraceStore()
        config = KernelRuntimeConfig(event_export_port=store)
        event_log, projection, *_ = _build_services(config)

        # event_log is wrapped, projection._event_log is the unwrapped inner
        assert isinstance(event_log, EventExportingEventLog)
        assert isinstance(projection._event_log, InMemoryKernelRuntimeEventLog)

    def test_export_fires_on_append_via_kernel_runtime_config(self) -> None:
        """End-to-end: appending via the wrapped event_log triggers export."""
        from agent_kernel.runtime.kernel_runtime import KernelRuntimeConfig, _build_services

        store = InMemoryRunTraceStore()
        config = KernelRuntimeConfig(event_export_port=store)
        event_log, *_ = _build_services(config)

        commit = _make_commit("run-wire", event_types=["run.started", "run.completed"])

        async def _run() -> None:
            await event_log.append_action_commit(commit)
            await asyncio.sleep(0)  # let background task execute

        asyncio.run(_run())

        trace = store.get("run-wire")
        assert trace is not None
        assert trace.is_terminal
        assert trace.terminal_state == "completed"
