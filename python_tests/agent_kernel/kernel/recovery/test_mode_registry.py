"""Tests for RecoveryModeRegistry and KERNEL_RECOVERY_MODE_REGISTRY (P4a)."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.recovery.mode_registry import (
    KERNEL_RECOVERY_MODE_REGISTRY,
    RecoveryModeRegistry,
)


class TestRecoveryModeRegistry:
    def test_register_and_get(self) -> None:
        registry = RecoveryModeRegistry()
        registry.register("my_action", "my_mode")
        assert registry.get("my_action") == "my_mode"

    def test_get_unknown_returns_none(self) -> None:
        registry = RecoveryModeRegistry()
        assert registry.get("nonexistent") is None

    def test_duplicate_registration_raises(self) -> None:
        registry = RecoveryModeRegistry()
        registry.register("my_action", "mode_a")
        with pytest.raises(ValueError, match="my_action"):
            registry.register("my_action", "mode_b")

    def test_known_actions_returns_registered_actions(self) -> None:
        registry = RecoveryModeRegistry()
        registry.register("action_x", "mode_x")
        registry.register("action_y", "mode_y")
        assert registry.known_actions() == frozenset({"action_x", "action_y"})

    def test_known_actions_is_immutable(self) -> None:
        registry = RecoveryModeRegistry()
        actions = registry.known_actions()
        assert isinstance(actions, frozenset)


class TestKernelRecoveryModeRegistry:
    """Verifies that the built-in mappings are pre-populated."""

    def test_schedule_compensation_maps_to_static_compensation(self) -> None:
        assert KERNEL_RECOVERY_MODE_REGISTRY.get("schedule_compensation") == "static_compensation"

    def test_notify_human_operator_maps_to_human_escalation(self) -> None:
        assert KERNEL_RECOVERY_MODE_REGISTRY.get("notify_human_operator") == "human_escalation"

    def test_abort_run_maps_to_abort(self) -> None:
        assert KERNEL_RECOVERY_MODE_REGISTRY.get("abort_run") == "abort"

    def test_unknown_planner_action_returns_none(self) -> None:
        assert KERNEL_RECOVERY_MODE_REGISTRY.get("__nonexistent__") is None


class TestGateUsesRegistry:
    """Integration test: gate uses KERNEL_RECOVERY_MODE_REGISTRY for mode resolution."""

    def test_gate_raises_for_unregistered_plan_action(self) -> None:
        """Verify gate raises ValueError for actions not in the registry."""
        import asyncio

        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

        class _BrokenPlanner:
            def build_plan_from_input(self, recovery_input: object) -> object:
                class _FakePlan:
                    action = "__unknown_custom_action__"
                    reason = "test"
                    compensation_action_id = None
                    escalation_channel_ref = None
                    run_id = "run-1"

                return _FakePlan()

        gate = PlannedRecoveryGateService(planner=_BrokenPlanner())  # type: ignore[arg-type]

        from agent_kernel.kernel.contracts import RecoveryInput, RunProjection

        recovery_input = RecoveryInput(
            run_id="run-1",
            reason_code="test",
            lifecycle_state="dispatching",
            projection=RunProjection(
                run_id="run-1",
                lifecycle_state="dispatching",
                projected_offset=1,
                waiting_external=False,
                ready_for_dispatch=True,
            ),
            reflection_round=0,
        )
        with pytest.raises(ValueError, match="__unknown_custom_action__"):
            asyncio.run(gate.decide(recovery_input))
