"""Context adapter for binding and exporting runtime context.

Design intent:
  - Convert heterogeneous platform context metadata into
    normalized binding DTOs used by kernel execution components.
  - Preserve agent_kernel separation of concerns: adapter performs
    *mapping only*.

Architectural boundary:
  - No admission decisions here.
  - No side effects here.
  - No lifecycle/event truth updates here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentCoreContextInput:
    """Represents context input from agent-core platform metadata.

    Attributes:
        session_id: Session identifier for context binding scope.
        context_ref: Optional context reference for hot context
            binding.
        context_json: Optional context payload for inline context
            injection.

    """

    session_id: str
    context_ref: str | None = None
    context_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeContextBinding:
    """Represents one resolved context binding for kernel execution.

    Attributes:
        binding_ref: Stable reference for the bound context.
        hot_context_ref: Hot context reference for immediate access.
        warm_context_ref: Warm context reference for deferred access.
        content_hash: Stable context content hash for snapshot governance.
        workspace_rules_md_ref: Optional rules markdown reference.
        workspace_summary_md_ref: Optional summary markdown reference.

    """

    binding_ref: str
    hot_context_ref: str | None = None
    warm_context_ref: str | None = None
    content_hash: str | None = None
    workspace_rules_md_ref: str | None = None
    workspace_summary_md_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AgentCoreContextExport:
    """Represents exported context data for one run.

    Attributes:
        run_id: Run identifier that owns the exported context.
        context_ref: Context reference for the exported data.
        summary_ref: Optional summary reference for the context.

    """

    run_id: str
    context_ref: str
    summary_ref: str | None = None


class AgentCoreContextAdapter:
    """Maps agent-core context metadata to kernel context bindings.

    The adapter accepts agent-core style context inputs and produces
    normalized binding DTOs for downstream kernel consumption.

    Attributes:
        _binding_by_run: Mapping from run_id to binding_ref.
        _context_by_binding: Mapping from binding_ref to context_ref.

    """

    def __init__(self) -> None:
        """Initialize in-memory run/context binding indexes."""
        self._binding_by_run: dict[str, str] = {}
        self._context_by_binding: dict[str, str] = {}

    def bind_context(
        self,
        input_value: AgentCoreContextInput,
    ) -> RuntimeContextBinding:
        """Binds context from agent-core input into kernel binding.

        Args:
            input_value: Agent-core context input carrying session
                and context metadata.

        Returns:
            A resolved context binding for kernel execution.

        """
        binding_ref = f"ctx:{input_value.session_id}"
        if input_value.context_ref is not None:
            self._context_by_binding[binding_ref] = input_value.context_ref
        content_hash = _build_context_content_hash(input_value)
        return RuntimeContextBinding(
            binding_ref=binding_ref,
            hot_context_ref=input_value.context_ref,
            warm_context_ref=input_value.context_ref,
            content_hash=content_hash,
            workspace_rules_md_ref=f"ctx-rules:{input_value.session_id}",
            workspace_summary_md_ref=f"ctx-summary:{input_value.session_id}",
        )

    def bind_run_context(
        self,
        run_id: str,
        binding_ref: str,
    ) -> None:
        """Binds a context reference to a specific run.

        Args:
            run_id: Run identifier to bind context to.
            binding_ref: Context binding reference to associate.

        """
        self._binding_by_run[run_id] = binding_ref

    def resolve_run_context(self, run_id: str) -> str | None:
        """Return the binding_ref associated with a run, or None.

        Used by ``KernelFacade.spawn_child_run()`` to inherit the parent
        run's context binding into the child workflow start request.

        Args:
            run_id: Run identifier to look up.

        Returns:
            The binding_ref string if found, else None.

        """
        return self._binding_by_run.get(run_id)

    def export_context(
        self,
        run_id: str,
    ) -> AgentCoreContextExport:
        """Export context data associated with one run.

        Args:
            run_id: Run identifier whose context should be exported.

        Returns:
            An exported context snapshot for the specified run.

        """
        binding_ref = self._binding_by_run.get(run_id, "")
        context_ref = self._context_by_binding.get(
            binding_ref,
            f"ctx:{run_id}",
        )
        return AgentCoreContextExport(
            run_id=run_id,
            context_ref=context_ref,
            summary_ref=f"ctx-summary:{run_id}",
        )


def _build_context_content_hash(input_value: AgentCoreContextInput) -> str:
    """Build a deterministic content hash from context input.

    Args:
        input_value: Context payload that should map to one stable hash.

    Returns:
        Hex-encoded SHA256 digest for canonicalized context content.

    """
    payload = {
        "session_id": input_value.session_id,
        "context_ref": input_value.context_ref,
        "context_json": input_value.context_json or {},
    }
    canonical_text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
