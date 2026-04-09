"""SQLite-backed projection snapshot cache for cross-restart projection seeding."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.kernel.contracts import (
    DecisionProjectionService,
    RunPolicyVersions,
    RunProjection,
)


class ProjectionSnapshotCache:
    """Persists RunProjection snapshots in SQLite for fast warm-start seeding.

    On process restart the in-memory projection cache is empty.  This store
    allows a ``CachedDecisionProjectionService`` to seed the in-memory layer
    from the last persisted snapshot, avoiding a full event replay from
    offset 0.
    """

    _CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS projection_snapshots (
            run_id           TEXT    PRIMARY KEY,
            projected_offset INTEGER NOT NULL,
            snapshot_json    TEXT    NOT NULL,
            updated_at       TEXT    NOT NULL
        )
    """

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialize the cache and create the schema if absent.

        Args:
            database_path: SQLite file path.  Use ``":memory:"`` for
                in-process ephemeral storage (useful in tests).

        """
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.execute(self._CREATE_TABLE)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    def save(self, projection: RunProjection) -> None:
        """Serialize and upsert a RunProjection snapshot.

        Args:
            projection: The projection to persist.

        """
        snapshot_json = json.dumps(_projection_to_dict(projection), sort_keys=True)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO projection_snapshots
                    (run_id, projected_offset, snapshot_json, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(run_id) DO UPDATE SET
                    projected_offset = excluded.projected_offset,
                    snapshot_json    = excluded.snapshot_json,
                    updated_at       = excluded.updated_at
                """,
                (projection.run_id, projection.projected_offset, snapshot_json),
            )
            self._conn.commit()

    def load(self, run_id: str) -> RunProjection | None:
        """Load a cached projection snapshot.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Deserialized ``RunProjection``, or ``None`` if no snapshot exists.

        """
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM projection_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _dict_to_projection(json.loads(row[0]))

    def delete(self, run_id: str) -> None:
        """Remove a cached snapshot.

        Args:
            run_id: Run identifier whose snapshot should be deleted.

        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM projection_snapshots WHERE run_id = ?",
                (run_id,),
            )
            self._conn.commit()


class CachedDecisionProjectionService:
    """Wraps an in-memory DecisionProjectionService with SQLite snapshot seeding.

    On ``get()`` the wrapper checks whether the inner service already has a
    cached projection for the requested run.  If not, it attempts to load a
    snapshot from the ``ProjectionSnapshotCache`` and seeds the inner service
    before delegating.

    On ``catch_up()`` the wrapper persists the resulting projection to the
    cache after the inner service finishes its replay.
    """

    def __init__(
        self,
        inner: DecisionProjectionService,
        cache: ProjectionSnapshotCache,
    ) -> None:
        """Initialize the cached wrapper.

        Args:
            inner: The in-memory projection service to delegate to.
            cache: SQLite snapshot cache for cross-restart seeding.

        """
        self._inner = inner
        self._cache = cache

    async def catch_up(self, run_id: str, through_offset: int) -> RunProjection:
        """Catch up via the inner service and persist the result.

        Args:
            run_id: Run identifier to catch up.
            through_offset: Target offset to replay through.

        Returns:
            Updated projection at or past ``through_offset``.

        """
        self._maybe_seed(run_id)
        projection = await self._inner.catch_up(run_id, through_offset)
        self._cache.save(projection)
        return projection

    async def readiness(self, run_id: str, required_offset: int) -> bool:
        """Delegate readiness check to the inner service.

        Args:
            run_id: Run identifier to check.
            required_offset: Minimum required offset.

        Returns:
            ``True`` when projection has reached the required offset.

        """
        self._maybe_seed(run_id)
        return await self._inner.readiness(run_id, required_offset)

    async def get(self, run_id: str) -> RunProjection:
        """Return projection, seeding from SQLite if the inner cache is cold.

        Args:
            run_id: Run identifier to project.

        Returns:
            Current authoritative projection for the run.

        """
        self._maybe_seed(run_id)
        return await self._inner.get(run_id)

    def _maybe_seed(self, run_id: str) -> None:
        """Seed the inner service's in-memory cache from SQLite if cold."""
        # Only seed when the inner service has no entry yet.
        inner_cache: dict | None = getattr(self._inner, "_projection_by_run", None)
        if inner_cache is not None and run_id not in inner_cache:
            cached = self._cache.load(run_id)
            if cached is not None:
                inner_cache[run_id] = cached


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _projection_to_dict(projection: RunProjection) -> dict:
    """Convert a frozen RunProjection to a plain dict for JSON storage."""
    d = asdict(projection)
    # policy_versions is a nested frozen dataclass; asdict already
    # converts it to a dict, but we keep None passthrough explicit.
    return d


def _dict_to_projection(d: dict) -> RunProjection:
    """Reconstruct a RunProjection from a deserialized dict."""
    pv = d.get("policy_versions")
    policy_versions = RunPolicyVersions(**pv) if pv is not None else None
    return RunProjection(
        run_id=d["run_id"],
        lifecycle_state=d["lifecycle_state"],
        projected_offset=d["projected_offset"],
        waiting_external=d["waiting_external"],
        ready_for_dispatch=d["ready_for_dispatch"],
        current_action_id=d.get("current_action_id"),
        recovery_mode=d.get("recovery_mode"),
        recovery_reason=d.get("recovery_reason"),
        active_child_runs=d.get("active_child_runs", []),
        policy_versions=policy_versions,
        task_contract_ref=d.get("task_contract_ref"),
    )
