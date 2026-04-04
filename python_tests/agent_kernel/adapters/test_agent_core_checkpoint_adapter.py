"""Tests for checkpoint adapter mapping between platform and kernel views."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.adapters.agent_core.checkpoint_adapter import (
    AgentCoreCheckpointAdapter,
    AgentCoreResumeInput,
)
from agent_kernel.kernel.contracts import RunProjection


def test_export_checkpoint_view_maps_projection_without_leaking_storage_details() -> None:
    """Checkpoint adapter should expose a platform-safe checkpoint view."""
    adapter = AgentCoreCheckpointAdapter()
    adapter.bind_projection(
        RunProjection(
            run_id="run-1",
            lifecycle_state="waiting_external",
            projected_offset=8,
            waiting_external=True,
            ready_for_dispatch=False,
        )
    )

    checkpoint = asyncio.run(adapter.export_checkpoint_view("run-1"))
    assert checkpoint.run_id == "run-1"
    assert checkpoint.projected_offset == 8
    assert checkpoint.lifecycle_state == "waiting_external"
    assert checkpoint.snapshot_id == "snapshot:run-1:8"


def test_import_resume_request_preserves_run_identity_and_snapshot() -> None:
    """Checkpoint adapter should only map resume input into kernel-safe request."""
    adapter = AgentCoreCheckpointAdapter()
    resume_request = asyncio.run(
        adapter.import_resume_request(
            AgentCoreResumeInput(
                run_id="run-2",
                snapshot_id="snapshot:run-2:13",
            )
        )
    )

    assert resume_request.run_id == "run-2"
    assert resume_request.snapshot_id == "snapshot:run-2:13"
    assert resume_request.snapshot_offset == 13


def test_import_resume_request_allows_resume_without_snapshot_id() -> None:
    """Checkpoint adapter should preserve backward-compatible empty snapshot resume."""
    adapter = AgentCoreCheckpointAdapter()
    resume_request = asyncio.run(
        adapter.import_resume_request(
            AgentCoreResumeInput(
                run_id="run-2",
                snapshot_id=None,
            )
        )
    )

    assert resume_request.run_id == "run-2"
    assert resume_request.snapshot_id is None
    assert resume_request.snapshot_offset is None


def test_parse_snapshot_id_returns_run_id_and_offset() -> None:
    """Checkpoint adapter should parse deterministic snapshot identifiers."""
    run_id, offset = AgentCoreCheckpointAdapter.parse_snapshot_id("snapshot:run-3:21")
    assert run_id == "run-3"
    assert offset == 21


def test_parse_snapshot_id_allows_colon_in_run_id() -> None:
    """Checkpoint adapter should parse lineage run ids that include ':' characters."""
    run_id, offset = AgentCoreCheckpointAdapter.parse_snapshot_id("snapshot:session-1:research:21")
    assert run_id == "session-1:research"
    assert offset == 21


@pytest.mark.parametrize(
    "snapshot_id",
    [
        "",
        "snapshot",
        "snapshot:run-only",
        "snapshot::1",
        "snap:run-1:1",
        "snapshot:run-1:-1",
        "snapshot:run-1:not-a-number",
    ],
)
def test_parse_snapshot_id_rejects_invalid_format(snapshot_id: str) -> None:
    """Checkpoint adapter should reject malformed snapshot identifiers."""
    with pytest.raises(ValueError):
        AgentCoreCheckpointAdapter.parse_snapshot_id(snapshot_id)


def test_import_resume_request_raises_for_snapshot_run_mismatch() -> None:
    """Checkpoint adapter should fail fast when snapshot run_id mismatches request."""
    adapter = AgentCoreCheckpointAdapter()
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            adapter.import_resume_request(
                AgentCoreResumeInput(
                    run_id="run-5",
                    snapshot_id="snapshot:run-999:8",
                )
            )
        )
