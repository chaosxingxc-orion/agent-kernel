"""Red tests for the agent-core adapter boundaries."""

from __future__ import annotations

import asyncio

from agent_kernel.adapters.agent_core.checkpoint_adapter import (
    AgentCoreCheckpointAdapter,
    AgentCoreResumeInput,
)
from agent_kernel.adapters.agent_core.context_adapter import AgentCoreContextAdapter
from agent_kernel.adapters.agent_core.runner_adapter import (
    AgentCoreChildSpawnInput,
    AgentCoreRunnerAdapter,
    AgentCoreRunnerStartInput,
)
from agent_kernel.adapters.agent_core.session_adapter import (
    AgentCoreCallbackInput,
    AgentCoreSessionAdapter,
)
from agent_kernel.adapters.agent_core.tool_mcp_adapter import AgentCoreToolMCPAdapter
from agent_kernel.kernel.contracts import Action, RunProjection


def test_runner_adapter_maps_runner_request_without_inventing_kernel_truth() -> None:
    """RunnerAdapter should only translate platform input into a start request."""

    adapter = AgentCoreRunnerAdapter()

    request = adapter.from_runner_start(
        AgentCoreRunnerStartInput(
            runner_kind="deep_research_runner",
            session_id="session-1",
            goal_json={"goal": "summarize docs"},
            context_ref="ctx-1",
        )
    )

    assert request.initiator == "agent_core_runner"
    assert request.run_kind == "deep_research_runner"
    assert request.session_id == "session-1"
    assert request.context_ref == "ctx-1"


def test_runner_adapter_maps_child_spawn_request_verbatim() -> None:
    """RunnerAdapter should translate child spawn requests without side effects."""

    adapter = AgentCoreRunnerAdapter()

    request = adapter.from_runner_child_spawn(
        AgentCoreChildSpawnInput(
            parent_run_id="run-parent",
            runner_kind="verification_runner",
            child_goal_json={"artifact": "report"},
        )
    )

    assert request.parent_run_id == "run-parent"
    assert request.child_kind == "verification_runner"
    assert request.input_json == {"artifact": "report"}


def test_session_adapter_translates_callback_to_kernel_signal() -> None:
    """SessionAdapter should convert session callbacks into kernel signals only."""

    adapter = AgentCoreSessionAdapter()

    request = adapter.translate_callback(
        AgentCoreCallbackInput(
            session_id="session-1",
            callback_type="tool_result",
            callback_payload={"ok": True},
        )
    )

    assert request.signal_type == "tool_result"
    assert request.signal_payload == {"ok": True}


def test_session_adapter_binding_kind_persists_with_dedup_and_cross_kind_support() -> None:
    """SessionAdapter should dedup per kind while allowing cross-kind coexistence.

    This verifies the expected binding semantics:
      - repeated bind of same run under same kind is idempotent,
      - same run can be stored under another kind independently,
      - aggregated resolve keeps bind order for backward compatibility.
    """
    adapter = AgentCoreSessionAdapter()
    session_id = "session-kind-1"

    # First bind for primary kind.
    asyncio.run(adapter.bind_run_to_session(session_id, "run-1", "primary"))
    # Same pair (run, kind) should not duplicate.
    asyncio.run(adapter.bind_run_to_session(session_id, "run-1", "primary"))
    # Same run under a different kind should be accepted.
    asyncio.run(adapter.bind_run_to_session(session_id, "run-1", "child"))
    asyncio.run(adapter.bind_run_to_session(session_id, "run-2", "child"))

    all_runs = asyncio.run(adapter.resolve_session_run(session_id))
    assert all_runs == ["run-1", "run-1", "run-2"]

    primary_runs = asyncio.run(adapter.resolve_session_run_by_kind(session_id, "primary"))
    child_runs = asyncio.run(adapter.resolve_session_run_by_kind(session_id, "child"))
    recovery_runs = asyncio.run(adapter.resolve_session_run_by_kind(session_id, "recovery"))

    assert primary_runs == ["run-1"]
    assert child_runs == ["run-1", "run-2"]
    assert recovery_runs == []


def test_session_adapter_kind_query_keeps_session_isolation() -> None:
    """Kind-based resolve should return only runs of the target session."""
    adapter = AgentCoreSessionAdapter()

    asyncio.run(adapter.bind_run_to_session("session-a", "run-a-primary", "primary"))
    asyncio.run(adapter.bind_run_to_session("session-b", "run-b-primary", "primary"))
    asyncio.run(adapter.bind_run_to_session("session-b", "run-b-child", "child"))

    assert asyncio.run(
        adapter.resolve_session_run_by_kind("session-a", "primary")
    ) == ["run-a-primary"]
    assert asyncio.run(adapter.resolve_session_run_by_kind("session-a", "child")) == []


def test_adapters_expose_kernel_boundary_protocol_methods() -> None:
    """Agent-core adapters should expose protocol-aligned boundary methods."""
    runner_adapter = AgentCoreRunnerAdapter()
    session_adapter = AgentCoreSessionAdapter()
    tool_mcp_adapter = AgentCoreToolMCPAdapter()
    checkpoint_adapter = AgentCoreCheckpointAdapter()
    context_adapter = AgentCoreContextAdapter()

    for adapter, methods in (
        (runner_adapter, ("from_runner_start",)),
        (session_adapter, ("from_session_signal", "from_callback")),
        (
            tool_mcp_adapter,
            (
                "resolve_tool_bindings",
                "resolve_mcp_bindings",
                "resolve_skill_bindings",
                "resolve_declarative_bundle",
            ),
        ),
        (checkpoint_adapter, ("export_checkpoint", "import_resume")),
        (context_adapter, ("bind_context",)),
    ):
        for method_name in methods:
            assert hasattr(adapter, method_name), f"{type(adapter).__name__}.{method_name}"


def test_session_adapter_exposes_ingress_alias_methods() -> None:
    """Session adapter should expose ingress-compatible alias methods."""
    adapter = AgentCoreSessionAdapter()
    callback = AgentCoreCallbackInput(
        session_id="session-1",
        callback_type="tool_result",
        callback_payload={"ok": True},
    )
    signal_request = adapter.from_session_signal(callback)
    callback_request = adapter.from_callback(callback)
    assert signal_request.signal_type == "tool_result"
    assert callback_request.signal_type == "tool_result"


def test_checkpoint_adapter_exposes_checkpoint_resume_port_aliases() -> None:
    """Checkpoint adapter should support CheckpointResumePort method names."""
    adapter = AgentCoreCheckpointAdapter()
    adapter.bind_projection(
        RunProjection(
            run_id="run-1",
            lifecycle_state="ready",
            projected_offset=9,
            waiting_external=False,
            ready_for_dispatch=True,
        )
    )

    checkpoint_view = asyncio.run(adapter.export_checkpoint("run-1"))
    resume_request = asyncio.run(
        adapter.import_resume(
            AgentCoreResumeInput(run_id="run-1", snapshot_id="snapshot:run-1:9")
        )
    )
    assert checkpoint_view.projected_offset == 9
    assert resume_request.snapshot_offset == 9


def test_tool_mcp_adapter_exposes_capability_adapter_methods() -> None:
    """Tool/MCP adapter should expose capability adapter-compatible methods."""
    adapter = AgentCoreToolMCPAdapter()
    action = Action(
        action_id="action-1",
        run_id="run-1",
        action_type="tool.search",
        effect_class="read_only",
        input_json={
            "tool_name": "search",
            "mcp": {"server_name": "docs", "capability_id": "query"},
            "skill_bindings": ["skill.a", "skill.b"],
            "declarative_bundle_digest": {
                "bundle_ref": "bundle-1",
                "semantics_version": "v1",
                "content_hash": "hash-c",
                "compile_hash": "hash-k",
            },
        },
    )

    assert len(asyncio.run(adapter.resolve_tool_bindings(action))) == 1
    assert len(asyncio.run(adapter.resolve_mcp_bindings(action))) == 1
    assert asyncio.run(adapter.resolve_skill_bindings(action)) == ["skill.a", "skill.b"]
    declarative_bundle = asyncio.run(adapter.resolve_declarative_bundle(action))
    assert declarative_bundle is not None
    assert declarative_bundle["bundle_ref"] == "bundle-1"
