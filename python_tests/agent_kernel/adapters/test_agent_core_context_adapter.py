"""Verifies for context adapter mapping between platform and kernel runtime."""

from __future__ import annotations

from agent_kernel.adapters.agent_core.context_adapter import (
    AgentCoreContextAdapter,
    AgentCoreContextInput,
)


def test_bind_context_generates_binding_ref_and_preserves_context_refs() -> None:
    """Context adapter should return deterministic binding view for kernel runtime."""
    adapter = AgentCoreContextAdapter()

    binding = adapter.bind_context(
        AgentCoreContextInput(
            session_id="session-1",
            context_ref="ctx-1",
            context_json={"goal": "collect sources"},
        )
    )

    assert binding.binding_ref
    assert binding.hot_context_ref == "ctx-1"
    assert binding.warm_context_ref == "ctx-1"
    assert binding.content_hash
    assert binding.workspace_rules_md_ref == "ctx-rules:session-1"
    assert binding.workspace_summary_md_ref == "ctx-summary:session-1"


def test_export_context_returns_last_bound_context_for_run() -> None:
    """Context adapter should export run-level context summary after binding."""
    adapter = AgentCoreContextAdapter()

    binding = adapter.bind_context(
        AgentCoreContextInput(session_id="session-2", context_ref="ctx-2")
    )
    adapter.bind_run_context("run-2", binding.binding_ref)

    export_view = adapter.export_context("run-2")
    assert export_view.run_id == "run-2"
    assert export_view.context_ref == "ctx-2"
    assert export_view.summary_ref == "ctx-summary:run-2"
