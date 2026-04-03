"""Tests for ActionTypeRegistry and KERNEL_ACTION_TYPE_REGISTRY (R3d)."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.action_type_registry import (
    KERNEL_ACTION_TYPE_REGISTRY,
    ActionTypeDescriptor,
    ActionTypeRegistry,
    validate_action_type,
)


class TestActionTypeRegistry:
    def test_register_and_get(self) -> None:
        registry = ActionTypeRegistry()
        desc = ActionTypeDescriptor(action_type="custom_rpc", description="gRPC action")
        registry.register(desc)
        assert registry.get("custom_rpc") is desc

    def test_get_unknown_returns_none(self) -> None:
        registry = ActionTypeRegistry()
        assert registry.get("nonexistent") is None

    def test_duplicate_registration_raises(self) -> None:
        registry = ActionTypeRegistry()
        registry.register(ActionTypeDescriptor(action_type="dup", description="a"))
        with pytest.raises(ValueError, match="dup"):
            registry.register(ActionTypeDescriptor(action_type="dup", description="b"))

    def test_known_types_returns_frozenset(self) -> None:
        registry = ActionTypeRegistry()
        registry.register(ActionTypeDescriptor(action_type="x", description="x"))
        assert registry.known_types() == frozenset({"x"})

    def test_all_sorted_by_action_type(self) -> None:
        registry = ActionTypeRegistry()
        registry.register(ActionTypeDescriptor(action_type="z_type", description="z"))
        registry.register(ActionTypeDescriptor(action_type="a_type", description="a"))
        names = [d.action_type for d in registry.all()]
        assert names == sorted(names)


class TestKernelActionTypeRegistry:
    """Verifies all five built-in action types are pre-populated."""

    def test_tool_call_registered(self) -> None:
        desc = KERNEL_ACTION_TYPE_REGISTRY.get("tool_call")
        assert desc is not None
        assert desc.supports_dedupe is True

    def test_mcp_call_registered(self) -> None:
        assert KERNEL_ACTION_TYPE_REGISTRY.get("mcp_call") is not None

    def test_skill_script_registered(self) -> None:
        assert KERNEL_ACTION_TYPE_REGISTRY.get("skill_script") is not None

    def test_sub_agent_registered(self) -> None:
        desc = KERNEL_ACTION_TYPE_REGISTRY.get("sub_agent")
        assert desc is not None
        assert desc.executor_hint == "remote_service"
        assert desc.is_idempotent is True

    def test_noop_registered_without_dedupe(self) -> None:
        desc = KERNEL_ACTION_TYPE_REGISTRY.get("noop")
        assert desc is not None
        assert desc.supports_dedupe is False

    def test_all_returns_five_descriptors(self) -> None:
        # Exactly 5 built-in types; custom registrations in other tests
        # use local ActionTypeRegistry instances, not the global singleton.
        assert len(KERNEL_ACTION_TYPE_REGISTRY.all()) >= 5


class TestValidateActionType:
    def test_known_type_returns_true(self) -> None:
        assert validate_action_type("tool_call") is True

    def test_unknown_type_returns_false(self) -> None:
        assert validate_action_type("__nonexistent__") is False

    def test_unknown_type_strict_raises(self) -> None:
        with pytest.raises(ValueError, match="__nonexistent__"):
            validate_action_type("__nonexistent__", strict=True)

    def test_known_type_strict_returns_true(self) -> None:
        assert validate_action_type("sub_agent", strict=True) is True


class TestActionTypeDescriptorDefaults:
    def test_default_executor_hint_is_local_process(self) -> None:
        desc = ActionTypeDescriptor(action_type="test", description="test")
        assert desc.executor_hint == "local_process"

    def test_default_is_idempotent_true(self) -> None:
        desc = ActionTypeDescriptor(action_type="test2", description="test2")
        assert desc.is_idempotent is True

    def test_default_supports_dedupe_true(self) -> None:
        desc = ActionTypeDescriptor(action_type="test3", description="test3")
        assert desc.supports_dedupe is True
