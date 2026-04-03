"""Tests for ScriptRuntimeRegistry (R4a)."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.kernel.cognitive.script_runtime_registry import (
    KERNEL_SCRIPT_RUNTIME_REGISTRY,
    ScriptRuntimeDescriptor,
    ScriptRuntimeRegistry,
    validate_host_kind,
)
from agent_kernel.kernel.contracts import ScriptActivityInput, ScriptResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(host_kind: str = "echo") -> ScriptActivityInput:
    return ScriptActivityInput(
        run_id="run-1",
        action_id="act-1",
        script_id="s-1",
        script_content="",
        host_kind=host_kind,
    )


class _StubRuntime:
    """Minimal stub runtime that returns a fixed result."""

    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[ScriptActivityInput] = []
        self._exit_code = exit_code

    async def execute_script(self, input_value: ScriptActivityInput) -> ScriptResult:
        self.calls.append(input_value)
        return ScriptResult(
            script_id=input_value.script_id,
            exit_code=self._exit_code,
            stdout="stub",
            stderr="",
            output_json=None,
            execution_ms=1,
        )


# ---------------------------------------------------------------------------
# ScriptRuntimeDescriptor
# ---------------------------------------------------------------------------


class TestScriptRuntimeDescriptor:
    def test_is_frozen(self) -> None:
        desc = ScriptRuntimeDescriptor(host_kind="echo", description="test")
        with pytest.raises((AttributeError, TypeError)):
            desc.host_kind = "other"  # type: ignore[misc]

    def test_default_not_production_safe(self) -> None:
        desc = ScriptRuntimeDescriptor(host_kind="echo", description="test")
        assert desc.is_safe_for_production is False

    def test_supports_timeout_default_true(self) -> None:
        desc = ScriptRuntimeDescriptor(host_kind="echo", description="test")
        assert desc.supports_timeout is True


# ---------------------------------------------------------------------------
# ScriptRuntimeRegistry — registration
# ---------------------------------------------------------------------------


class TestScriptRuntimeRegistryRegistration:
    def test_register_and_get(self) -> None:
        reg = ScriptRuntimeRegistry()
        stub = _StubRuntime()
        reg.register("my_kind", stub, description="test stub")
        assert reg.get("my_kind") is stub

    def test_get_unknown_returns_none(self) -> None:
        reg = ScriptRuntimeRegistry()
        assert reg.get("nonexistent") is None

    def test_known_host_kinds_empty_initially(self) -> None:
        reg = ScriptRuntimeRegistry()
        assert reg.known_host_kinds() == []

    def test_known_host_kinds_after_register(self) -> None:
        reg = ScriptRuntimeRegistry()
        reg.register("a", _StubRuntime())
        reg.register("b", _StubRuntime())
        assert set(reg.known_host_kinds()) == {"a", "b"}

    def test_register_overwrites_existing(self) -> None:
        reg = ScriptRuntimeRegistry()
        stub1 = _StubRuntime(exit_code=0)
        stub2 = _StubRuntime(exit_code=1)
        reg.register("kind", stub1)
        reg.register("kind", stub2)
        assert reg.get("kind") is stub2

    def test_get_descriptor_returns_descriptor(self) -> None:
        reg = ScriptRuntimeRegistry()
        reg.register("k", _StubRuntime(), description="d", is_safe_for_production=True)
        desc = reg.get_descriptor("k")
        assert desc is not None
        assert desc.host_kind == "k"
        assert desc.description == "d"
        assert desc.is_safe_for_production is True

    def test_get_descriptor_unknown_returns_none(self) -> None:
        reg = ScriptRuntimeRegistry()
        assert reg.get_descriptor("unknown") is None

    def test_all_descriptors(self) -> None:
        reg = ScriptRuntimeRegistry()
        reg.register("x", _StubRuntime())
        reg.register("y", _StubRuntime())
        descriptors = reg.all_descriptors()
        kinds = {d.host_kind for d in descriptors}
        assert kinds == {"x", "y"}


# ---------------------------------------------------------------------------
# ScriptRuntimeRegistry — dispatch
# ---------------------------------------------------------------------------


class TestScriptRuntimeRegistryDispatch:
    def test_dispatch_routes_to_correct_runtime(self) -> None:
        reg = ScriptRuntimeRegistry()
        stub = _StubRuntime(exit_code=42)
        reg.register("custom", stub)
        result = asyncio.run(reg.dispatch(_make_input(host_kind="custom")))
        assert result.exit_code == 42
        assert len(stub.calls) == 1

    def test_dispatch_unknown_raises_key_error(self) -> None:
        reg = ScriptRuntimeRegistry()
        with pytest.raises(KeyError, match="host_kind"):
            asyncio.run(reg.dispatch(_make_input(host_kind="unknown_kind")))

    def test_dispatch_error_message_includes_host_kind(self) -> None:
        reg = ScriptRuntimeRegistry()
        with pytest.raises(KeyError) as exc_info:
            asyncio.run(reg.dispatch(_make_input(host_kind="mystery")))
        assert "mystery" in str(exc_info.value)

    def test_dispatch_passes_full_input(self) -> None:
        reg = ScriptRuntimeRegistry()
        stub = _StubRuntime()
        reg.register("echo", stub)
        inp = _make_input(host_kind="echo")
        asyncio.run(reg.dispatch(inp))
        assert stub.calls[0] is inp


# ---------------------------------------------------------------------------
# KERNEL_SCRIPT_RUNTIME_REGISTRY — built-in runtimes
# ---------------------------------------------------------------------------


class TestKernelScriptRuntimeRegistry:
    def test_echo_is_registered(self) -> None:
        assert "echo" in KERNEL_SCRIPT_RUNTIME_REGISTRY.known_host_kinds()

    def test_in_process_python_is_registered(self) -> None:
        assert "in_process_python" in KERNEL_SCRIPT_RUNTIME_REGISTRY.known_host_kinds()

    def test_local_process_is_registered(self) -> None:
        assert "local_process" in KERNEL_SCRIPT_RUNTIME_REGISTRY.known_host_kinds()

    def test_echo_descriptor_not_production_safe(self) -> None:
        desc = KERNEL_SCRIPT_RUNTIME_REGISTRY.get_descriptor("echo")
        assert desc is not None
        assert desc.is_safe_for_production is False

    def test_local_process_descriptor_production_safe(self) -> None:
        desc = KERNEL_SCRIPT_RUNTIME_REGISTRY.get_descriptor("local_process")
        assert desc is not None
        assert desc.is_safe_for_production is True

    def test_dispatch_echo_succeeds(self) -> None:
        inp = ScriptActivityInput(
            run_id="r",
            action_id="a",
            script_id="s",
            script_content="",
            host_kind="echo",
            parameters={"k": "v"},
        )
        result = asyncio.run(KERNEL_SCRIPT_RUNTIME_REGISTRY.dispatch(inp))
        assert result.exit_code == 0
        assert result.output_json == {"k": "v"}

    def test_dispatch_in_process_python_succeeds(self) -> None:
        inp = ScriptActivityInput(
            run_id="r",
            action_id="a",
            script_id="s",
            script_content="print('hi')",
            host_kind="in_process_python",
        )
        result = asyncio.run(KERNEL_SCRIPT_RUNTIME_REGISTRY.dispatch(inp))
        assert result.exit_code == 0
        assert "hi" in result.stdout

    def test_custom_runtime_can_be_registered(self) -> None:
        """Third-party code can inject a new host_kind."""
        import uuid

        reg = ScriptRuntimeRegistry()
        stub = _StubRuntime(exit_code=7)
        custom_kind = f"custom_{uuid.uuid4().hex[:6]}"
        reg.register(custom_kind, stub, description="third-party runtime")
        result = asyncio.run(reg.dispatch(_make_input(host_kind=custom_kind)))
        assert result.exit_code == 7


# ---------------------------------------------------------------------------
# validate_host_kind
# ---------------------------------------------------------------------------


class TestValidateHostKind:
    def test_known_kind_returns_true(self) -> None:
        assert validate_host_kind("echo") is True

    def test_unknown_kind_returns_false(self) -> None:
        assert validate_host_kind("totally_unknown") is False

    def test_strict_raises_on_unknown(self) -> None:
        with pytest.raises(ValueError, match="host_kind"):
            validate_host_kind("not_registered", strict=True)

    def test_strict_does_not_raise_on_known(self) -> None:
        assert validate_host_kind("in_process_python", strict=True) is True
