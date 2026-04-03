"""Checkpoint adapter for platform-safe checkpoint and resume mappings.

This adapter wraps kernel projection state into platform-facing checkpoint
views and maps platform resume requests back to kernel-safe request payloads.
It intentionally performs no lifecycle mutation and no recovery decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import RunLifecycleState, RunProjection


@dataclass(frozen=True, slots=True)
class AgentCoreCheckpointView:
    """Represents platform-facing checkpoint summary for one run.

    Attributes:
        run_id: Kernel run identifier for the checkpoint.
        snapshot_id: Optional snapshot identity string.
        projected_offset: Offset of the projected state.
        lifecycle_state: Current lifecycle state of the run.
    """

    run_id: str
    snapshot_id: str | None
    projected_offset: int
    lifecycle_state: RunLifecycleState


@dataclass(frozen=True, slots=True)
class AgentCoreResumeInput:
    """Represents one resume request from platform layer.

    Attributes:
        run_id: Kernel run identifier to resume.
        snapshot_id: Optional snapshot identity for offset targeting.
    """

    run_id: str
    snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class KernelResumeRequest:
    """Represents kernel-safe resume request mapped from platform input.

    Attributes:
        run_id: Kernel run identifier to resume.
        snapshot_id: Optional snapshot identity for offset targeting.
        snapshot_offset: Optional parsed offset from snapshot identity.
    """

    run_id: str
    snapshot_id: str | None = None
    snapshot_offset: int | None = None


class AgentCoreCheckpointAdapter:
    """Maps checkpoint and resume data between platform and kernel contracts."""

    def __init__(self) -> None:
        self._projection_by_run: dict[str, RunProjection] = {}

    def bind_projection(self, projection: RunProjection) -> None:
        """Binds latest projection snapshot for one run.

        Args:
            projection: Current run projection to bind.
        """
        self._projection_by_run[projection.run_id] = projection

    async def export_checkpoint_view(self, run_id: str) -> AgentCoreCheckpointView:
        """Exports checkpoint summary from bound projection state.

        Args:
            run_id: Run identifier to export checkpoint for.

        Returns:
            Platform-facing checkpoint view with lifecycle state.
        """
        projection = self._projection_by_run.get(run_id)
        if projection is None:
            return AgentCoreCheckpointView(
                run_id=run_id,
                snapshot_id=None,
                projected_offset=0,
                lifecycle_state="created",
            )
        snapshot_id = f"snapshot:{run_id}:{projection.projected_offset}"
        return AgentCoreCheckpointView(
            run_id=run_id,
            snapshot_id=snapshot_id,
            projected_offset=projection.projected_offset,
            lifecycle_state=projection.lifecycle_state,
        )

    async def export_checkpoint(self, run_id: str) -> AgentCoreCheckpointView:
        """CheckpointResumePort-compatible alias.
        Args:
            run_id: (description)
        Returns:
            AgentCoreCheckpointView: (description)
        """
        return await self.export_checkpoint_view(run_id)

    @staticmethod
    def parse_snapshot_id(snapshot_id: str) -> tuple[str, int]:
        """Parses adapter-level snapshot identity into run and offset.

        This parser only validates the transport format used by the adapter
        boundary (`snapshot:<run_id>:<offset>`). It is intentionally *not* the
        authority for kernel lifecycle correctness, replay permission, or
        storage existence.

        Args:
            snapshot_id: Snapshot identifier from the platform payload.

        Returns:
            A tuple of `(run_id, offset)` parsed from `snapshot_id`.

        Raises:
            ValueError: If `snapshot_id` does not match the expected format.
        """
        if not snapshot_id.startswith("snapshot:"):
            raise ValueError("Invalid snapshot_id format. Expected 'snapshot:<run_id>:<offset>'.")

        # run_id may itself contain ":" (for example parent/child lineage ids).
        # The adapter therefore treats the final ":" as the offset separator and
        # keeps the full middle section as run_id.
        body = snapshot_id[len("snapshot:") :]
        run_id, separator, offset_value = body.rpartition(":")
        if separator != ":":
            raise ValueError("Invalid snapshot_id format. Expected 'snapshot:<run_id>:<offset>'.")
        if not run_id:
            raise ValueError("Invalid snapshot_id: run_id must be non-empty.")
        if not offset_value.isdigit():
            raise ValueError("Invalid snapshot_id: offset must be a non-negative integer.")
        return run_id, int(offset_value)

    async def import_resume_request(
        self,
        input_value: AgentCoreResumeInput,
    ) -> KernelResumeRequest:
        """Maps platform resume payload into kernel-safe resume request.

        The adapter performs minimal, deterministic validation for the
        `snapshot_id` transport format and run identity consistency. This is a
        boundary-level sanity check only, not kernel authority over resume
        validity.

        Args:
            input_value: Resume payload provided by the platform layer.

        Returns:
            Kernel resume request to pass into kernel APIs.

        Raises:
            ValueError: If `snapshot_id` cannot be parsed, or if the parsed
                run identifier does not match `input_value.run_id`.
        """
        snapshot_offset: int | None = None
        if input_value.snapshot_id is not None:
            parsed_run_id, parsed_offset = self.parse_snapshot_id(input_value.snapshot_id)
            if parsed_run_id != input_value.run_id:
                raise ValueError("snapshot_id run_id does not match resume request run_id.")
            snapshot_offset = parsed_offset
        return KernelResumeRequest(
            run_id=input_value.run_id,
            snapshot_id=input_value.snapshot_id,
            snapshot_offset=snapshot_offset,
        )

    async def import_resume(self, input_value: AgentCoreResumeInput) -> KernelResumeRequest:
        """CheckpointResumePort-compatible alias.
        Args:
            input_value: (description)
        Returns:
            KernelResumeRequest: (description)
        """
        return await self.import_resume_request(input_value)
