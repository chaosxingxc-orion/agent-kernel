"""Verifies for sqlite-backed kernel runtime event log persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
from agent_kernel.kernel.persistence.sqlite_event_log import SQLiteKernelRuntimeEventLog


def _build_event(
    run_id: str,
    event_id: str,
    event_type: str,
    commit_offset: int,
    payload_json: dict[str, object] | None = None,
) -> RuntimeEvent:
    """Builds one runtime event test fixture."""
    return RuntimeEvent(
        run_id=run_id,
        event_id=event_id,
        commit_offset=commit_offset,
        event_type=event_type,
        event_class="fact",
        event_authority="authoritative_fact",
        ordering_key=run_id,
        wake_policy="wake_actor",
        created_at="2026-03-31T00:00:00Z",
        payload_json=payload_json,
    )


def _build_commit(run_id: str, commit_id: str, events: list[RuntimeEvent]) -> ActionCommit:
    """Builds one action commit test fixture."""
    return ActionCommit(
        run_id=run_id,
        commit_id=commit_id,
        events=events,
        created_at="2026-03-31T00:00:00Z",
    )


def _build_event_log(database_path: Path) -> SQLiteKernelRuntimeEventLog:
    """Builds one SQLite event log bound to a database file."""
    return SQLiteKernelRuntimeEventLog(database_path)


def test_append_commit_assigns_offsets_and_preserves_event_input_order(tmp_path: Path) -> None:
    """Append should assign sequential offsets while preserving input event ordering."""
    event_log = _build_event_log(tmp_path / "events.db")
    commit = _build_commit(
        run_id="run-1",
        commit_id="c1",
        events=[
            _build_event("run-1", "evt-1", "run.created", commit_offset=99),
            _build_event("run-1", "evt-2", "run.ready", commit_offset=-1),
            _build_event(
                "run-1",
                "evt-3",
                "run.waiting_external",
                commit_offset=42,
                payload_json={"signal": "awaiting_callback"},
            ),
        ],
    )

    commit_ref = asyncio.run(event_log.append_action_commit(commit))
    loaded_events = asyncio.run(event_log.load("run-1"))

    assert commit_ref == "commit-ref-1"
    assert [event.commit_offset for event in loaded_events] == [1, 2, 3]
    assert [event.event_type for event in loaded_events] == [
        "run.created",
        "run.ready",
        "run.waiting_external",
    ]
    assert loaded_events[2].payload_json == {"signal": "awaiting_callback"}
    event_log.close()


def test_load_orders_by_run_and_offset_with_after_offset_filter(tmp_path: Path) -> None:
    """Load should return one run stream in ascending offset order after a lower bound."""
    event_log = _build_event_log(tmp_path / "events.db")

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-1",
                "c1",
                [
                    _build_event("run-1", "evt-1", "run.created", commit_offset=10),
                    _build_event("run-1", "evt-2", "run.ready", commit_offset=11),
                ],
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit("run-2", "c2", [_build_event("run-2", "evt-3", "run.created", 3)])
        )
    )

    run_1_tail = asyncio.run(event_log.load("run-1", after_offset=1))
    run_2_events = asyncio.run(event_log.load("run-2"))

    assert [event.commit_offset for event in run_1_tail] == [2]
    assert [event.event_type for event in run_1_tail] == ["run.ready"]
    assert [event.commit_offset for event in run_2_events] == [1]
    assert [event.event_type for event in run_2_events] == ["run.created"]
    event_log.close()


def test_append_uses_storage_offsets_across_multiple_commits(tmp_path: Path) -> None:
    """Append should ignore event commit offsets provided by callers."""
    event_log = _build_event_log(tmp_path / "events.db")

    first_ref = asyncio.run(
        event_log.append_action_commit(
            _build_commit("run-9", "c1", [_build_event("run-9", "evt-1", "run.created", 101)])
        )
    )
    second_ref = asyncio.run(
        event_log.append_action_commit(
            _build_commit("run-9", "c2", [_build_event("run-9", "evt-2", "run.ready", -77)])
        )
    )
    loaded_events = asyncio.run(event_log.load("run-9"))

    assert first_ref == "commit-ref-1"
    assert second_ref == "commit-ref-2"
    assert [event.commit_offset for event in loaded_events] == [1, 2]
    event_log.close()


def test_append_rejects_empty_commit(tmp_path: Path) -> None:
    """Append should reject commits without runtime events."""
    event_log = _build_event_log(tmp_path / "events.db")
    empty_commit = _build_commit("run-empty", "c-empty", events=[])

    with pytest.raises(
        ValueError,
        match=r"ActionCommit\.events must contain at least one event\.",
    ):
        asyncio.run(event_log.append_action_commit(empty_commit))

    event_log.close()


def test_offsets_continue_after_reopening_database(tmp_path: Path) -> None:
    """Offset assignment should continue from durable state after reopening."""
    database_path = tmp_path / "events.db"
    first_log = _build_event_log(database_path)
    first_ref = asyncio.run(
        first_log.append_action_commit(
            _build_commit(
                "run-persist",
                "c1",
                [_build_event("run-persist", "evt-1", "run.created", 0)],
            )
        )
    )
    first_log.close()

    reopened_log = _build_event_log(database_path)
    second_ref = asyncio.run(
        reopened_log.append_action_commit(
            _build_commit(
                "run-persist",
                "c2",
                [_build_event("run-persist", "evt-2", "run.ready", 0)],
            )
        )
    )
    loaded_events = asyncio.run(reopened_log.load("run-persist"))

    assert first_ref == "commit-ref-1"
    assert second_ref == "commit-ref-2"
    assert [event.commit_offset for event in loaded_events] == [1, 2]
    assert [event.event_type for event in loaded_events] == ["run.created", "run.ready"]
    reopened_log.close()
