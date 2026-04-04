"""Tests for SQLite-backed RecoveryOutcome store persistence semantics."""

from __future__ import annotations

import asyncio

from agent_kernel.kernel.contracts import RecoveryOutcome
from agent_kernel.kernel.persistence.sqlite_recovery_outcome_store import (
    SQLiteRecoveryOutcomeStore,
)


def _build_outcome(
    run_id: str,
    written_at: str,
    *,
    action_id: str | None,
    recovery_mode: str = "human_escalation",
    outcome_state: str = "escalated",
) -> RecoveryOutcome:
    return RecoveryOutcome(
        run_id=run_id,
        action_id=action_id,
        recovery_mode=recovery_mode,
        outcome_state=outcome_state,
        written_at=written_at,
        operator_escalation_ref="ops://pager",
        emitted_event_ids=["evt-1"],
    )


def test_sqlite_recovery_outcome_store_returns_none_when_empty(tmp_path) -> None:
    store = SQLiteRecoveryOutcomeStore(tmp_path / "recovery-empty.sqlite3")
    try:
        assert asyncio.run(store.latest_for_run("run-empty")) is None
    finally:
        store.close()


def test_sqlite_recovery_outcome_store_persists_across_reopen(tmp_path) -> None:
    database_path = tmp_path / "recovery-persist.sqlite3"
    store = SQLiteRecoveryOutcomeStore(database_path)
    try:
        asyncio.run(
            store.write_outcome(
                _build_outcome(
                    "run-1",
                    "2026-04-01T00:00:00Z",
                    action_id="action-1",
                )
            )
        )
    finally:
        store.close()

    reopened = SQLiteRecoveryOutcomeStore(database_path)
    try:
        latest = asyncio.run(reopened.latest_for_run("run-1"))
        assert latest is not None
        assert latest.run_id == "run-1"
        assert latest.action_id == "action-1"
        assert latest.emitted_event_ids == ["evt-1"]
    finally:
        reopened.close()


def test_sqlite_recovery_outcome_store_latest_for_run_uses_newest_written_at(tmp_path) -> None:
    store = SQLiteRecoveryOutcomeStore(tmp_path / "recovery-latest.sqlite3")
    try:
        asyncio.run(
            store.write_outcome(
                _build_outcome(
                    "run-2",
                    "2026-04-01T00:00:00Z",
                    action_id="action-old",
                )
            )
        )
        asyncio.run(
            store.write_outcome(
                _build_outcome(
                    "run-2",
                    "2026-04-01T00:10:00Z",
                    action_id="action-new",
                    recovery_mode="static_compensation",
                    outcome_state="executed",
                )
            )
        )

        latest = asyncio.run(store.latest_for_run("run-2"))
        assert latest is not None
        assert latest.action_id == "action-new"
        assert latest.recovery_mode == "static_compensation"
        assert latest.outcome_state == "executed"
    finally:
        store.close()
