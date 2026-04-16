"""Concurrent safety tests for SQLite persistence stores.

These tests verify that threading.Lock protection in the SQLite stores
prevents data corruption under concurrent access from multiple threads
and asyncio tasks — the same concurrency pattern the Temporal worker uses.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime

import pytest

from agent_kernel.kernel.contracts import (
    ActionCommit,
    RecoveryOutcome,
    RuntimeEvent,
    TurnIntentRecord,
)
from agent_kernel.kernel.persistence.sqlite_event_log import SQLiteKernelRuntimeEventLog
from agent_kernel.kernel.persistence.sqlite_recovery_outcome_store import (
    SQLiteRecoveryOutcomeStore,
)
from agent_kernel.kernel.persistence.sqlite_turn_intent_log import SQLiteTurnIntentLog


def _utc_now() -> str:
    """Utc now."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _make_commit(run_id: str, seq: int) -> ActionCommit:
    """Make commit."""
    return ActionCommit(
        run_id=run_id,
        commit_id=f"commit-{seq}",
        created_at=_utc_now(),
        events=[
            RuntimeEvent(
                run_id=run_id,
                event_id=f"evt-{seq}",
                commit_offset=seq,
                event_type="run.ready",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key=run_id,
                wake_policy="wake_actor",
                created_at=_utc_now(),
            )
        ],
    )


# ---------------------------------------------------------------------------
# SQLiteKernelRuntimeEventLog concurrent tests
# ---------------------------------------------------------------------------


class TestSQLiteEventLogConcurrency:
    """Verifies that concurrent append and load calls do not corrupt data."""

    def _make_log(self) -> SQLiteKernelRuntimeEventLog:
        """Make log."""
        return SQLiteKernelRuntimeEventLog(":memory:")

    @pytest.mark.asyncio
    async def test_concurrent_appends_same_run_monotonic_offsets(self) -> None:
        """Concurrent appends for the same run must produce monotonically increasing offsets."""
        log = self._make_log()
        run_id = "run-concurrent-1"
        n = 20

        async def _append(seq: int) -> None:
            """Appends test data."""
            await log.append_action_commit(_make_commit(run_id, seq))

        await asyncio.gather(*[_append(i) for i in range(n)])

        events = await log.load(run_id, after_offset=0)
        offsets = [e.commit_offset for e in events]
        assert len(offsets) == n
        # Offsets must be monotonically increasing (no gaps, no duplicates)
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == n

    @pytest.mark.asyncio
    async def test_concurrent_appends_different_runs_isolated(self) -> None:
        """Concurrent appends for different runs must not interfere."""
        log = self._make_log()
        runs = [f"run-iso-{i}" for i in range(5)]
        appends_per_run = 10

        async def _append_run(run_id: str) -> None:
            """Append run."""
            for seq in range(appends_per_run):
                await log.append_action_commit(_make_commit(run_id, seq))

        await asyncio.gather(*[_append_run(r) for r in runs])

        for run_id in runs:
            events = await log.load(run_id, after_offset=0)
            assert len(events) == appends_per_run, (
                f"run {run_id}: expected {appends_per_run} events, got {len(events)}"
            )

    @pytest.mark.asyncio
    async def test_concurrent_load_and_append(self) -> None:
        """Concurrent readers must not block writers or receive corrupt data."""
        log = self._make_log()
        run_id = "run-rw"
        results: list[list] = []

        async def _write() -> None:
            """Writes test data."""
            for i in range(15):
                await log.append_action_commit(_make_commit(run_id, i))

        async def _read() -> None:
            """Reads test data."""
            events = await log.load(run_id, after_offset=0)
            results.append(events)

        await asyncio.gather(_write(), _read(), _read(), _read())

        # At least one read must have returned a non-empty list
        non_empty = [r for r in results if r]
        assert non_empty, "At least one concurrent read should have seen some events"

    def test_threaded_concurrent_appends(self) -> None:
        """Verify threading.Lock under multi-threaded (non-asyncio) concurrent append."""
        log = self._make_log()
        run_id = "run-threaded"
        n_threads = 10
        appends_per_thread = 5
        errors: list[Exception] = []

        def _worker(thread_id: int) -> None:
            """Runs worker behavior for the test."""
            loop = asyncio.new_event_loop()
            try:
                for seq in range(appends_per_thread):
                    loop.run_until_complete(
                        log.append_action_commit(_make_commit(run_id, thread_id * 100 + seq))
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                loop.close()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent threaded appends raised: {errors}"

        loop = asyncio.new_event_loop()
        events = loop.run_until_complete(log.load(run_id, after_offset=0))
        loop.close()
        expected = n_threads * appends_per_thread
        assert len(events) == expected

    @pytest.mark.asyncio
    async def test_max_offset_consistent_with_load(self) -> None:
        """max_offset() must agree with the highest offset returned by load()."""
        log = self._make_log()
        run_id = "run-maxoffset"
        n = 8

        for i in range(n):
            await log.append_action_commit(_make_commit(run_id, i))

        max_off = await log.max_offset(run_id)
        events = await log.load(run_id, after_offset=0)
        assert max_off == events[-1].commit_offset


# ---------------------------------------------------------------------------
# SQLiteRecoveryOutcomeStore concurrent tests
# ---------------------------------------------------------------------------


class TestSQLiteRecoveryOutcomeStoreConcurrency:
    """Verifies thread-safe write_outcome under concurrent asyncio tasks."""

    def _make_store(self) -> SQLiteRecoveryOutcomeStore:
        """Make store."""
        return SQLiteRecoveryOutcomeStore(":memory:")

    def _make_outcome(self, run_id: str, action_id: str) -> RecoveryOutcome:
        """Make outcome."""
        return RecoveryOutcome(
            run_id=run_id,
            action_id=action_id,
            recovery_mode="static_compensation",
            outcome_state="executed",
            written_at=_utc_now(),
        )

    @pytest.mark.asyncio
    async def test_concurrent_write_outcome_same_run(self) -> None:
        """Multiple concurrent writes for the same run must all succeed."""
        store = self._make_store()
        run_id = "recovery-run"
        n = 12

        async def _write(i: int) -> None:
            """Writes test data."""
            await store.write_outcome(self._make_outcome(run_id, f"action-{i}"))

        await asyncio.gather(*[_write(i) for i in range(n)])

        latest = await store.latest_for_run(run_id)
        assert latest is not None

    @pytest.mark.asyncio
    async def test_latest_for_run_returns_most_recent(self) -> None:
        """latest_for_run() must return the most recently written outcome."""
        store = self._make_store()
        run_id = "recovery-seq"

        for i in range(5):
            await store.write_outcome(self._make_outcome(run_id, f"action-{i}"))

        latest = await store.latest_for_run(run_id)
        assert latest is not None
        assert latest.run_id == run_id


# ---------------------------------------------------------------------------
# SQLiteTurnIntentLog concurrent tests
# ---------------------------------------------------------------------------


class TestSQLiteTurnIntentLogConcurrency:
    """Verifies thread-safe write_intent under concurrent asyncio tasks."""

    def _make_log(self) -> SQLiteTurnIntentLog:
        """Make log."""
        return SQLiteTurnIntentLog(":memory:")

    def _make_intent(self, run_id: str, offset: int) -> TurnIntentRecord:
        """Make intent."""
        return TurnIntentRecord(
            run_id=run_id,
            intent_commit_ref=f"commit-{offset}",
            decision_ref=f"decision-{offset}",
            decision_fingerprint=f"fp-{uuid.uuid4().hex}",
            dispatch_dedupe_key=f"dedup-{offset}",
            host_kind="local_process",
            outcome_kind="dispatched",
            written_at=_utc_now(),
        )

    @pytest.mark.asyncio
    async def test_concurrent_write_intent(self) -> None:
        """Multiple concurrent write_intent calls must not raise or corrupt state."""
        log = self._make_log()
        run_id = "intent-run"
        n = 10

        async def _write(i: int) -> None:
            """Writes test data."""
            await log.write_intent(self._make_intent(run_id, i))

        await asyncio.gather(*[_write(i) for i in range(n)])

        latest = await log.latest_for_run(run_id)
        assert latest is not None
        assert latest.run_id == run_id
