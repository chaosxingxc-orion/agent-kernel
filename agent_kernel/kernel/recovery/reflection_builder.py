"""ReflectionContextBuilder for reflect_and_retry recovery turns.

Builds an enriched ContextWindow that the model can use to analyse a failure
and produce a corrected script or action.
"""

from __future__ import annotations

from agent_kernel.kernel.contracts import (
    BranchResult,
    ContextWindow,
    ScriptFailureEvidence,
)


class ReflectionContextBuilder:
    """Builds ContextWindow increment for reflect_and_retry turns.

    Takes failure evidence, successful parallel branches, and a base context
    window, and produces an enriched ContextWindow the model can understand.
    The enrichment is inserted into the ``recovery_context`` field of the
    returned window; all other fields from the base context are preserved.
    """

    def build(
        self,
        evidence: ScriptFailureEvidence,
        successful_branches: list[BranchResult],
        base_context: ContextWindow,
        reflection_round: int = 1,
    ) -> ContextWindow:
        """Merges failure evidence into base context as recovery_context.

        The ``recovery_context`` dict contains:
        - ``failure_kind``: ``evidence.failure_kind``
        - ``suspected_cause``: ``evidence.suspected_cause``
        - ``budget_consumed_ratio``: ``evidence.budget_consumed_ratio``
        - ``original_script``: ``evidence.original_script``
        - ``partial_output``: ``evidence.partial_output``
        - ``stderr_tail``: ``evidence.stderr_tail``
        - ``successful_branch_ids``: ``[b.action_id for b in successful_branches]``
        - ``reflection_round``: ``reflection_round``
        - ``instruction``: a fixed human-readable directive for the model.

        All other fields from ``base_context`` are preserved unchanged.

        Args:
            evidence: Structured failure evidence from a failed script execution.
            successful_branches: List of successfully completed parallel branches.
            base_context: The base context window to enrich.
            reflection_round: Current reflection round number (1-indexed).

        Returns:
            New ``ContextWindow`` with ``recovery_context`` populated.
        """
        recovery_context: dict = {
            "failure_kind": evidence.failure_kind,
            "suspected_cause": evidence.suspected_cause,
            "budget_consumed_ratio": evidence.budget_consumed_ratio,
            "original_script": evidence.original_script,
            "partial_output": evidence.partial_output,
            "stderr_tail": evidence.stderr_tail,
            "successful_branch_ids": [b.action_id for b in successful_branches],
            "reflection_round": reflection_round,
            "instruction": "Analyse the failure and produce a corrected script",
        }

        return ContextWindow(
            system_instructions=base_context.system_instructions,
            tool_definitions=base_context.tool_definitions,
            skill_definitions=base_context.skill_definitions,
            history=base_context.history,
            current_state=base_context.current_state,
            memory_ref=base_context.memory_ref,
            recovery_context=recovery_context,
            inference_config=base_context.inference_config,
        )
