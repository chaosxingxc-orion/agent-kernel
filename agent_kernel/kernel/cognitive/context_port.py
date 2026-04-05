"""InMemory ContextPort implementation for tests and PoC development.

This module provides the PoC ``InMemoryContextPort`` that assembles a
``ContextWindow`` from a ``CapabilitySnapshot`` and runtime event history
without calling any external services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot

from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    RuntimeEvent,
    SkillSummary,
    ToolDefinition,
)


class InMemoryContextPort:
    """In-memory PoC ContextPort for tests and development.

    Assembles a minimal ``ContextWindow`` from a capability snapshot and
    event history.  Does NOT call external services.

    The assembled window is deliberately minimal 鈥?it is designed to keep
    tests hermetic and fast, not to produce production-quality prompts.
    """

    async def assemble(
        self,
        run_id: str,
        snapshot: CapabilitySnapshot,
        history: list[RuntimeEvent],
        inference_config: InferenceConfig | None = None,
        recovery_context: dict[str, Any] | None = None,
    ) -> ContextWindow:
        """Assembles one context window from snapshot and event history.

        Args:
            run_id: Run identifier for this context assembly.
            snapshot: Frozen capability snapshot for tool/skill enumeration.
            history: Ordered event history for conversation reconstruction.
            inference_config: Optional inference config to embed in the window.
            recovery_context: Optional structured recovery context for
                reflect_and_retry turns.

        Returns:
            Immutable ``ContextWindow`` ready for model inference.

        """
        system_instructions = self._build_system_instructions(snapshot)
        tool_definitions = self._build_tool_definitions(snapshot)
        skill_definitions = self._build_skill_definitions(snapshot)
        message_history = tuple(
            {"role": e.event_authority, "content": e.event_type} for e in history
        )
        current_state: dict[str, Any] = {"projected_events": len(history)}

        return ContextWindow(
            system_instructions=system_instructions,
            tool_definitions=tool_definitions,
            skill_definitions=skill_definitions,
            history=message_history,
            current_state=current_state,
            recovery_context=recovery_context,
            inference_config=inference_config,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system_instructions(snapshot: CapabilitySnapshot) -> str:
        """Build system instructions string from snapshot feature flags.

        Uses ``feature_flags`` as a PoC stand-in for capability scope
        directives.  Production implementations should replace this with
        a policy-resolved instruction set.

        Args:
            snapshot: Frozen capability snapshot.

        Returns:
            Newline-joined instruction string, or an empty string when no
            feature flags are present.

        """
        if not snapshot.feature_flags:
            return ""
        return "\n".join(snapshot.feature_flags)

    @staticmethod
    def _build_tool_definitions(snapshot: CapabilitySnapshot) -> tuple[ToolDefinition, ...]:
        """Build a tuple of ToolDefinition objects from snapshot tool_bindings.

        Each tool binding name is converted into a minimal ``ToolDefinition``
        with an empty schema so the model can be made aware of available tools
        without requiring a registry lookup.

        Args:
            snapshot: Frozen capability snapshot.

        Returns:
            Tuple of ``ToolDefinition`` objects (may be empty).

        """
        return tuple(
            ToolDefinition(
                name=name,
                description=f"Tool binding: {name}",
                input_schema={"type": "object", "properties": {}},
            )
            for name in snapshot.tool_bindings
        )

    @staticmethod
    def _build_skill_definitions(snapshot: CapabilitySnapshot) -> tuple[SkillSummary, ...]:
        """Build a tuple of SkillSummary objects from snapshot skill_bindings.

        Each skill binding reference is converted into a minimal
        ``SkillSummary``.

        Args:
            snapshot: Frozen capability snapshot.

        Returns:
            Tuple of ``SkillSummary`` objects (may be empty).

        """
        return tuple(
            SkillSummary(
                skill_id=ref,
                description=f"Skill binding: {ref}",
            )
            for ref in snapshot.skill_bindings
        )
