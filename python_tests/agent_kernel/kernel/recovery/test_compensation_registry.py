"""Tests for CompensationRegistry and its integration with PlannedRecoveryGateService."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from agent_kernel.kernel.contracts import (
    Action,
    RecoveryInput,
    RunProjection,
)
from agent_kernel.kernel.recovery.compensation_registry import (
    CompensationRegistry,
)
from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(effect_class: str = "compensatable_write") -> Action:
    return Action(
        action_id="act-1",
        run_id="run-1",
        action_type="tool_call",
        effect_class=effect_class,  # type: ignore[arg-type]
    )


def _make_recovery_input(
    run_id: str = "run-1",
    reason_code: str = "transient_failure",
    recovery_mode: str | None = None,
) -> RecoveryInput:
    projection = RunProjection(
        run_id=run_id,
        lifecycle_state="recovering",
        projected_offset=5,
        waiting_external=False,
        ready_for_dispatch=False,
        current_action_id="act-1",
        recovery_mode=recovery_mode,  # type: ignore[arg-type]
    )
    return RecoveryInput(
        run_id=run_id,
        reason_code=reason_code,
        lifecycle_state="recovering",
        projection=projection,
        failed_action_id="act-1",
    )


# ---------------------------------------------------------------------------
# CompensationRegistry unit tests
# ---------------------------------------------------------------------------


class TestCompensationRegistry:

    def test_empty_registry_has_no_handlers(self) -> None:
        registry = CompensationRegistry()
        assert registry.lookup("compensatable_write") is None
        assert not registry.has_handler("compensatable_write")
        assert registry.registered_effect_classes() == []

    def test_register_and_lookup(self) -> None:
        registry = CompensationRegistry()
        fn = AsyncMock()
        registry.register("compensatable_write", fn, description="undo write")

        entry = registry.lookup("compensatable_write")
        assert entry is not None
        assert entry.effect_class == "compensatable_write"
        assert entry.compensate is fn
        assert entry.description == "undo write"

    def test_has_handler_returns_true_after_register(self) -> None:
        registry = CompensationRegistry()
        registry.register("idempotent_write", AsyncMock())
        assert registry.has_handler("idempotent_write")
        assert not registry.has_handler("irreversible_write")

    def test_register_overwrites_previous_handler(self) -> None:
        registry = CompensationRegistry()
        fn_old = AsyncMock()
        fn_new = AsyncMock()
        registry.register("compensatable_write", fn_old)
        registry.register("compensatable_write", fn_new)
        assert registry.lookup("compensatable_write").compensate is fn_new

    def test_registered_effect_classes_sorted(self) -> None:
        registry = CompensationRegistry()
        registry.register("idempotent_write", AsyncMock())
        registry.register("compensatable_write", AsyncMock())
        registry.register("read_only", AsyncMock())
        assert registry.registered_effect_classes() == [
            "compensatable_write",
            "idempotent_write",
            "read_only",
        ]

    def test_handler_decorator_registers_function(self) -> None:
        registry = CompensationRegistry()

        @registry.handler("compensatable_write", description="via decorator")
        async def _undo(action: Any) -> None:
            pass

        entry = registry.lookup("compensatable_write")
        assert entry is not None
        assert entry.compensate is _undo
        assert entry.description == "via decorator"

    def test_handler_decorator_returns_original_function(self) -> None:
        registry = CompensationRegistry()

        async def _undo(action: Any) -> None:
            pass

        result = registry.handler("compensatable_write")(_undo)
        assert result is _undo

    def test_execute_calls_handler_with_action(self) -> None:
        registry = CompensationRegistry()
        called_with: list[Any] = []

        async def _undo(action: Any) -> None:
            called_with.append(action)

        registry.register("compensatable_write", _undo)
        action = _make_action("compensatable_write")
        result = asyncio.run(registry.execute(action))
        assert result is True
        assert called_with == [action]

    def test_execute_returns_false_for_unregistered_effect_class(self) -> None:
        registry = CompensationRegistry()
        action = _make_action("irreversible_write")
        result = asyncio.run(registry.execute(action))
        assert result is False

    def test_execute_swallows_handler_exception(self) -> None:
        registry = CompensationRegistry()

        async def _failing_undo(action: Any) -> None:
            raise RuntimeError("compensation exploded")

        registry.register("compensatable_write", _failing_undo)
        action = _make_action("compensatable_write")
        # Must not raise
        result = asyncio.run(registry.execute(action))
        assert result is True  # handler was found and invoked (even though it raised)

    def test_multiple_effect_classes_isolated(self) -> None:
        registry = CompensationRegistry()
        fn_a = AsyncMock()
        fn_b = AsyncMock()
        registry.register("compensatable_write", fn_a)
        registry.register("idempotent_write", fn_b)

        assert registry.lookup("compensatable_write").compensate is fn_a
        assert registry.lookup("idempotent_write").compensate is fn_b


# ---------------------------------------------------------------------------
# PlannedRecoveryGateService + CompensationRegistry integration
# ---------------------------------------------------------------------------


class TestPlannedRecoveryGateServiceWithRegistry:

    def test_no_registry_passes_through_compensation_decision(self) -> None:
        """Without a registry the gate never validates compensation handlers."""
        gate = PlannedRecoveryGateService()
        recovery_input = _make_recovery_input(
            reason_code="transient_failure",
            recovery_mode="static_compensation",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "static_compensation"

    def test_with_registry_allows_compensation_when_handler_registered(self) -> None:
        registry = CompensationRegistry()
        registry.register("compensatable_write", AsyncMock())
        gate = PlannedRecoveryGateService(compensation_registry=registry)

        # reason_code uses "effect_class:reason" convention so gate can extract it
        recovery_input = _make_recovery_input(
            reason_code="compensatable_write:transient_failure",
            recovery_mode="static_compensation",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "static_compensation"

    def test_with_registry_downgrades_to_abort_when_no_handler(self) -> None:
        registry = CompensationRegistry()
        # No handler registered for irreversible_write
        gate = PlannedRecoveryGateService(compensation_registry=registry)

        recovery_input = _make_recovery_input(
            reason_code="irreversible_write:transient_failure",
            recovery_mode="static_compensation",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "abort"
        assert "no_compensation_handler" in decision.reason

    def test_with_registry_does_not_affect_human_escalation(self) -> None:
        """Registry validation only applies to static_compensation mode."""
        registry = CompensationRegistry()  # empty registry
        gate = PlannedRecoveryGateService(compensation_registry=registry)

        recovery_input = _make_recovery_input(
            reason_code="human_requires_review",
            recovery_mode="human_escalation",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "human_escalation"

    def test_with_registry_does_not_affect_abort(self) -> None:
        """Registry validation does not interfere with abort decisions."""
        registry = CompensationRegistry()
        gate = PlannedRecoveryGateService(compensation_registry=registry)

        recovery_input = _make_recovery_input(
            reason_code="fatal_unrecoverable",
            recovery_mode="abort",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "abort"

    def test_compensation_registry_property_returns_injected_registry(self) -> None:
        registry = CompensationRegistry()
        gate = PlannedRecoveryGateService(compensation_registry=registry)
        assert gate.compensation_registry is registry

    def test_compensation_registry_property_none_when_not_injected(self) -> None:
        gate = PlannedRecoveryGateService()
        assert gate.compensation_registry is None

    def test_reason_without_colon_does_not_trigger_downgrade(self) -> None:
        """When reason_code has no ':' the effect_class is unknown; no downgrade."""
        registry = CompensationRegistry()  # empty
        gate = PlannedRecoveryGateService(compensation_registry=registry)

        # No colon in reason_code → _extract_effect_class returns None → no check
        recovery_input = _make_recovery_input(
            reason_code="transient_failure",
            recovery_mode="static_compensation",
        )
        decision = asyncio.run(gate.decide(recovery_input))
        # Gate cannot determine effect_class → passes through as compensation
        assert decision.mode == "static_compensation"


# ---------------------------------------------------------------------------
# R3e — Compensation execute() with DedupeStore idempotency
# ---------------------------------------------------------------------------


class TestCompensationRegistryDedupeIntegration:
    """Verifies at-most-once execution via DedupeStore."""

    def test_execute_without_dedupe_store_calls_handler(self) -> None:
        """Baseline: no dedupe_store → handler called once normally."""
        registry = CompensationRegistry()
        calls: list[Any] = []

        async def _handler(action: Any) -> None:
            calls.append(action.action_id)

        registry.register("write", _handler)
        result = asyncio.run(registry.execute(_make_action("write")))
        assert result is True
        assert calls == ["act-1"]

    def test_execute_with_dedupe_store_calls_handler_on_first_call(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        registry = CompensationRegistry()
        calls: list[str] = []

        async def _handler(action: Any) -> None:
            calls.append(action.action_id)

        registry.register("write", _handler)
        store = InMemoryDedupeStore()
        result = asyncio.run(registry.execute(_make_action("write"), dedupe_store=store))
        assert result is True
        assert calls == ["act-1"]

    def test_execute_with_dedupe_store_skips_handler_on_second_call(self) -> None:
        """Second execute() for the same action should not re-run the handler."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        registry = CompensationRegistry()
        calls: list[str] = []

        async def _handler(action: Any) -> None:
            calls.append(action.action_id)

        registry.register("write", _handler)
        store = InMemoryDedupeStore()
        action = _make_action("write")

        asyncio.run(registry.execute(action, dedupe_store=store))
        # Second call with same action — should be skipped
        asyncio.run(registry.execute(action, dedupe_store=store))
        assert len(calls) == 1

    def test_execute_marks_dedupe_acknowledged_after_success(self) -> None:
        """After success, the dedupe key should be in 'acknowledged' state."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        registry = CompensationRegistry()
        registry.register("write", AsyncMock())
        store = InMemoryDedupeStore()
        asyncio.run(registry.execute(_make_action("write"), dedupe_store=store))
        key = "compensation:write:act-1"
        record = store.get(key)
        assert record is not None
        assert record.state == "acknowledged"

    def test_different_actions_are_independent(self) -> None:
        """Different action_ids should each get their own idempotency slot."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        registry = CompensationRegistry()
        calls: list[str] = []

        async def _handler(action: Any) -> None:
            calls.append(action.action_id)

        registry.register("write", _handler)
        store = InMemoryDedupeStore()

        action1 = Action(
            action_id="act-1", run_id="run-1", action_type="tool_call", effect_class="write"
        )
        action2 = Action(
            action_id="act-2", run_id="run-1", action_type="tool_call", effect_class="write"
        )
        asyncio.run(registry.execute(action1, dedupe_store=store))
        asyncio.run(registry.execute(action2, dedupe_store=store))
        assert set(calls) == {"act-1", "act-2"}
