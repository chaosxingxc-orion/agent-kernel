"""Tests for SQLite-backed TurnIntentLog persistence semantics."""

from __future__ import annotations

import asyncio

from agent_kernel.kernel.contracts import TurnIntentRecord
from agent_kernel.kernel.persistence.sqlite_turn_intent_log import SQLiteTurnIntentLog


def _build_intent(
    run_id: str,
    intent_commit_ref: str,
    written_at: str,
    *,
    outcome_kind: str = "dispatched",
) -> TurnIntentRecord:
    return TurnIntentRecord(
        run_id=run_id,
        intent_commit_ref=intent_commit_ref,
        decision_ref=f"decision:{run_id}",
        decision_fingerprint=f"fp:{run_id}:{intent_commit_ref}",
        dispatch_dedupe_key=f"{run_id}:dedupe",
        host_kind="local_cli",
        outcome_kind=outcome_kind,
        written_at=written_at,
    )


def test_sqlite_turn_intent_log_returns_none_for_missing_run(tmp_path) -> None:
    store = SQLiteTurnIntentLog(tmp_path / "turn-intent-empty.sqlite3")
    try:
        assert asyncio.run(store.latest_for_run("run-missing")) is None
    finally:
        store.close()


def test_sqlite_turn_intent_log_persists_and_recovers_after_reopen(tmp_path) -> None:
    database_path = tmp_path / "turn-intent-persist.sqlite3"
    store = SQLiteTurnIntentLog(database_path)
    try:
        asyncio.run(
            store.write_intent(
                _build_intent(
                    "run-1",
                    "intent:action-1:10",
                    "2026-04-01T00:00:00Z",
                )
            )
        )
    finally:
        store.close()

    reopened = SQLiteTurnIntentLog(database_path)
    try:
        latest = asyncio.run(reopened.latest_for_run("run-1"))
        assert latest is not None
        assert latest.intent_commit_ref == "intent:action-1:10"
        assert latest.decision_ref == "decision:run-1"
    finally:
        reopened.close()


def test_sqlite_turn_intent_log_upserts_same_intent_commit_ref(tmp_path) -> None:
    store = SQLiteTurnIntentLog(tmp_path / "turn-intent-upsert.sqlite3")
    try:
        asyncio.run(
            store.write_intent(
                _build_intent(
                    "run-2",
                    "intent:action-1:10",
                    "2026-04-01T00:00:00Z",
                    outcome_kind="blocked",
                )
            )
        )
        asyncio.run(
            store.write_intent(
                _build_intent(
                    "run-2",
                    "intent:action-1:10",
                    "2026-04-01T00:05:00Z",
                    outcome_kind="dispatched",
                )
            )
        )

        latest = asyncio.run(store.latest_for_run("run-2"))
        assert latest is not None
        assert latest.intent_commit_ref == "intent:action-1:10"
        assert latest.outcome_kind == "dispatched"
        assert latest.written_at == "2026-04-01T00:05:00Z"
    finally:
        store.close()


def test_sqlite_turn_intent_log_latest_by_written_at(tmp_path) -> None:
    store = SQLiteTurnIntentLog(tmp_path / "turn-intent-latest.sqlite3")
    try:
        asyncio.run(
            store.write_intent(
                _build_intent(
                    "run-3",
                    "intent:action-1:10",
                    "2026-04-01T00:00:00Z",
                )
            )
        )
        asyncio.run(
            store.write_intent(
                _build_intent(
                    "run-3",
                    "intent:action-2:11",
                    "2026-04-01T00:10:00Z",
                )
            )
        )

        latest = asyncio.run(store.latest_for_run("run-3"))
        assert latest is not None
        assert latest.intent_commit_ref == "intent:action-2:11"
    finally:
        store.close()
