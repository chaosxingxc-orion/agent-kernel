"""Tests for InMemoryContextPort."""

from __future__ import annotations

import asyncio

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuilder,
    CapabilitySnapshotInput,
)
from agent_kernel.kernel.cognitive.context_port import InMemoryContextPort
from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    RuntimeEvent,
    TokenBudget,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    run_id: str = "run-1",
    tool_bindings: list[str] | None = None,
    skill_bindings: list[str] | None = None,
    feature_flags: list[str] | None = None,
) -> CapabilitySnapshot:
    """Builds a minimal CapabilitySnapshot for tests."""
    builder = CapabilitySnapshotBuilder()
    return builder.build(
        CapabilitySnapshotInput(
            run_id=run_id,
            based_on_offset=0,
            tenant_policy_ref="policy:test",
            permission_mode="strict",
            tool_bindings=tool_bindings or [],
            skill_bindings=skill_bindings or [],
            feature_flags=feature_flags or [],
        )
    )


def _make_event(run_id: str = "run-1", idx: int = 0) -> RuntimeEvent:
    """Builds a minimal RuntimeEvent for history tests."""
    return RuntimeEvent(
        run_id=run_id,
        event_id=f"ev-{idx}",
        commit_offset=idx,
        event_type=f"test_event_{idx}",
        event_class="fact",
        event_authority="authoritative_fact",
        ordering_key=f"ok-{idx}",
        wake_policy="wake_actor",
        created_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInMemoryContextPortBasic:
    """Basic assembly tests for InMemoryContextPort."""

    def test_assemble_returns_context_window(self) -> None:
        """assemble() should return a ContextWindow instance."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert isinstance(result, ContextWindow)

    def test_system_instructions_empty_when_no_feature_flags(self) -> None:
        """system_instructions should be empty when snapshot has no feature flags."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(feature_flags=[])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.system_instructions == ""

    def test_system_instructions_joined_from_feature_flags(self) -> None:
        """system_instructions should be newline-joined feature flags."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(feature_flags=["flag.a", "flag.b"])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert "flag.a" in result.system_instructions
        assert "flag.b" in result.system_instructions
        assert result.system_instructions == "flag.a\nflag.b"

    def test_tool_definitions_populated_from_tool_bindings(self) -> None:
        """tool_definitions should reflect snapshot.tool_bindings."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(tool_bindings=["tool.alpha", "tool.beta"])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert len(result.tool_definitions) == 2
        names = {td.name for td in result.tool_definitions}
        assert "tool.alpha" in names
        assert "tool.beta" in names

    def test_tool_definitions_empty_when_no_bindings(self) -> None:
        """tool_definitions should be empty tuple when no tool_bindings."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(tool_bindings=[])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.tool_definitions == ()

    def test_skill_definitions_populated_from_skill_bindings(self) -> None:
        """skill_definitions should reflect snapshot.skill_bindings."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(skill_bindings=["skill.plan", "skill.execute"])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert len(result.skill_definitions) == 2
        ids = {sd.skill_id for sd in result.skill_definitions}
        assert "skill.plan" in ids
        assert "skill.execute" in ids

    def test_skill_definitions_empty_when_no_bindings(self) -> None:
        """skill_definitions should be empty tuple when no skill_bindings."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(skill_bindings=[])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.skill_definitions == ()


class TestInMemoryContextPortHistory:
    """Tests for history assembly in InMemoryContextPort."""

    def test_history_empty_when_no_events(self) -> None:
        """history should be an empty tuple when history list is empty."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.history == ()

    def test_history_contains_one_entry_per_event(self) -> None:
        """history should have one dict per RuntimeEvent in the history list."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()
        events = [_make_event(idx=0), _make_event(idx=1)]

        result = asyncio.run(port.assemble("run-1", snapshot, events))

        assert len(result.history) == 2

    def test_history_entries_have_role_and_content(self) -> None:
        """Each history entry should have 'role' and 'content' keys."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()
        event = _make_event(idx=0)

        result = asyncio.run(port.assemble("run-1", snapshot, [event]))

        entry = result.history[0]
        assert "role" in entry
        assert "content" in entry
        assert entry["role"] == event.event_authority
        assert entry["content"] == event.event_type

    def test_current_state_reflects_event_count(self) -> None:
        """current_state['projected_events'] should equal len(history)."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()
        events = [_make_event(idx=i) for i in range(5)]

        result = asyncio.run(port.assemble("run-1", snapshot, events))

        assert result.current_state["projected_events"] == 5


class TestInMemoryContextPortOptionalFields:
    """Tests for optional fields (inference_config, recovery_context)."""

    def test_inference_config_passed_through(self) -> None:
        """inference_config should be propagated to the ContextWindow."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()
        config = InferenceConfig(
            model_ref="test-model",
            token_budget=TokenBudget(max_input=1024, max_output=256),
        )

        result = asyncio.run(port.assemble("run-1", snapshot, [], inference_config=config))

        assert result.inference_config is config

    def test_inference_config_none_by_default(self) -> None:
        """inference_config should be None when not provided."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.inference_config is None

    def test_recovery_context_passed_through(self) -> None:
        """recovery_context should be propagated to the ContextWindow."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()
        ctx = {"failure_code": "timeout", "attempt": 2}

        result = asyncio.run(port.assemble("run-1", snapshot, [], recovery_context=ctx))

        assert result.recovery_context == ctx

    def test_recovery_context_none_by_default(self) -> None:
        """recovery_context should be None when not provided."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot()

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        assert result.recovery_context is None

    def test_tool_definition_has_input_schema(self) -> None:
        """Each ToolDefinition should carry a non-empty input_schema dict."""
        port = InMemoryContextPort()
        snapshot = _make_snapshot(tool_bindings=["tool.x"])

        result = asyncio.run(port.assemble("run-1", snapshot, []))

        td = result.tool_definitions[0]
        assert isinstance(td.input_schema, dict)
        assert td.input_schema.get("type") == "object"
