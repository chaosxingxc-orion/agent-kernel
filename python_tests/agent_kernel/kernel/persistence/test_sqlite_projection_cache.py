"""Verifies for projectionsnapshotcache and cacheddecisionprojectionservice."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.contracts import RunPolicyVersions, RunProjection
from agent_kernel.kernel.minimal_runtime import (
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
)
from agent_kernel.kernel.persistence.sqlite_projection_cache import (
    CachedDecisionProjectionService,
    ProjectionSnapshotCache,
)


def _make_projection(
    run_id: str = "run-1",
    offset: int = 5,
    lifecycle_state: str = "ready",
) -> RunProjection:
    """Create a sample RunProjection for testing."""
    return RunProjection(
        run_id=run_id,
        lifecycle_state=lifecycle_state,
        projected_offset=offset,
        waiting_external=False,
        ready_for_dispatch=True,
        current_action_id="act-1",
        recovery_mode=None,
        recovery_reason=None,
        active_child_runs=["child-a", "child-b"],
        policy_versions=RunPolicyVersions(
            route_policy_version="v1",
            acceptance_policy_version="v2",
            pinned_at="2026-01-01T00:00:00Z",
        ),
        task_contract_ref="contract-ref-1",
    )


class TestProjectionSnapshotCache:
    """Test suite for ProjectionSnapshotCache."""

    def test_save_and_load_roundtrip(self) -> None:
        """Verifies save and load roundtrip."""
        cache = ProjectionSnapshotCache()
        proj = _make_projection()
        cache.save(proj)
        loaded = cache.load("run-1")
        assert loaded is not None
        assert loaded.run_id == proj.run_id
        assert loaded.projected_offset == proj.projected_offset
        assert loaded.lifecycle_state == proj.lifecycle_state
        assert loaded.waiting_external == proj.waiting_external
        assert loaded.ready_for_dispatch == proj.ready_for_dispatch
        assert loaded.current_action_id == proj.current_action_id
        assert loaded.active_child_runs == proj.active_child_runs
        assert loaded.task_contract_ref == proj.task_contract_ref
        assert loaded.policy_versions is not None
        assert loaded.policy_versions.route_policy_version == "v1"
        assert loaded.policy_versions.acceptance_policy_version == "v2"
        assert loaded.policy_versions.pinned_at == "2026-01-01T00:00:00Z"

    def test_load_missing_returns_none(self) -> None:
        """Verifies load missing returns none."""
        cache = ProjectionSnapshotCache()
        assert cache.load("nonexistent-run") is None

    def test_delete_removes_snapshot(self) -> None:
        """Verifies delete removes snapshot."""
        cache = ProjectionSnapshotCache()
        proj = _make_projection()
        cache.save(proj)
        assert cache.load("run-1") is not None
        cache.delete("run-1")
        assert cache.load("run-1") is None

    def test_save_upserts_on_conflict(self) -> None:
        """Verifies save upserts on conflict."""
        cache = ProjectionSnapshotCache()
        proj_v1 = _make_projection(offset=5)
        cache.save(proj_v1)
        proj_v2 = _make_projection(offset=10)
        cache.save(proj_v2)
        loaded = cache.load("run-1")
        assert loaded is not None
        assert loaded.projected_offset == 10


class TestCachedDecisionProjectionService:
    """Test suite for CachedDecisionProjectionService."""

    @pytest.mark.asyncio
    async def test_cached_service_seeds_from_sqlite(self) -> None:
        """After saving a projection to SQLite, a fresh service can load it."""
        cache = ProjectionSnapshotCache()
        proj = _make_projection(run_id="run-seed", offset=42)
        cache.save(proj)

        # Create a fresh in-memory projection service (empty cache).
        event_log = InMemoryKernelRuntimeEventLog()
        inner = InMemoryDecisionProjectionService(event_log)

        # Wrap it with the cached service.
        cached_svc = CachedDecisionProjectionService(inner, cache)

        # get() should seed from SQLite and return the cached projection.
        result = await cached_svc.get("run-seed")
        assert result.run_id == "run-seed"
        assert result.projected_offset == 42

        # Verify the inner service's in-memory cache was seeded.
        assert "run-seed" in inner._projection_by_run
