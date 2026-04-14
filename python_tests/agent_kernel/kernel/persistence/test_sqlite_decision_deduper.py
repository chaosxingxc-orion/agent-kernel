"""Tests for SQLiteDecisionDeduper — durable decision fingerprint deduplication."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.persistence.sqlite_decision_deduper import SQLiteDecisionDeduper


class TestSQLiteDecisionDeduper:
    """Unit tests using an in-memory SQLite database."""

    @pytest.fixture()
    def deduper(self) -> SQLiteDecisionDeduper:
        """Return a fresh in-memory deduper for each test."""
        d = SQLiteDecisionDeduper(database_path=":memory:")
        yield d
        d.close()

    @pytest.mark.asyncio
    async def test_contains_returns_false_before_add(self, deduper: SQLiteDecisionDeduper) -> None:
        """seen() returns False for an unknown fingerprint."""
        assert await deduper.seen("fp-unknown") is False

    @pytest.mark.asyncio
    async def test_contains_returns_true_after_add(self, deduper: SQLiteDecisionDeduper) -> None:
        """seen() returns True immediately after mark()."""
        await deduper.mark("fp-abc")
        assert await deduper.seen("fp-abc") is True

    @pytest.mark.asyncio
    async def test_add_is_idempotent(self, deduper: SQLiteDecisionDeduper) -> None:
        """Calling mark() twice for the same fingerprint does not raise."""
        await deduper.mark("fp-dup")
        await deduper.mark("fp-dup")  # must not raise
        assert await deduper.seen("fp-dup") is True

    @pytest.mark.asyncio
    async def test_different_fingerprints_tracked_independently(
        self, deduper: SQLiteDecisionDeduper
    ) -> None:
        """Marking one fingerprint does not affect other fingerprints."""
        await deduper.mark("fp-x")
        assert await deduper.seen("fp-x") is True
        assert await deduper.seen("fp-y") is False

    @pytest.mark.asyncio
    async def test_run_id_stored_and_optional(self, deduper: SQLiteDecisionDeduper) -> None:
        """run_id defaults to empty string and does not affect seen() semantics."""
        # mark without explicit run_id — defaults to ""
        await deduper.mark("fp-no-run")
        assert await deduper.seen("fp-no-run") is True

        # mark with an explicit run_id
        await deduper.mark("fp-with-run", run_id="run-42")
        assert await deduper.seen("fp-with-run") is True

        # verify the row was actually stored with the correct run_id
        row = deduper._conn.execute(
            "SELECT run_id FROM decision_fingerprints WHERE fingerprint = ?",
            ("fp-with-run",),
        ).fetchone()
        assert row is not None
        assert row["run_id"] == "run-42"


class TestSQLiteDecisionDeduперPersistence:
    """Integration tests verifying on-disk durability across deduper instances."""

    @pytest.mark.asyncio
    async def test_persists_across_instances(self, tmp_path: pytest.TempPathFactory) -> None:
        """Fingerprints marked by one instance are visible to a new instance on the same file."""
        db_path = tmp_path / "decision_deduper.db"

        # First instance: mark a fingerprint and close.
        first = SQLiteDecisionDeduper(database_path=db_path)
        await first.mark("fp-persist", run_id="run-1")
        first.close()

        # Second instance on the same file: fingerprint must already be seen.
        second = SQLiteDecisionDeduper(database_path=db_path)
        try:
            result = await second.seen("fp-persist")
        finally:
            second.close()

        assert result is True
