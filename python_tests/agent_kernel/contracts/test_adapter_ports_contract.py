"""Contract tests for kernel adapter port protocols."""

from __future__ import annotations

import agent_kernel.kernel.adapter_ports as adapter_ports


def test_ingress_adapter_protocol_exposes_required_methods() -> None:
    """IngressAdapter should define runner/session/callback translation entrypoints."""
    for method_name in ("from_runner_start", "from_session_signal", "from_callback"):
        assert hasattr(adapter_ports.IngressAdapter, method_name), method_name


def test_context_binding_port_protocol_exposes_bind_context() -> None:
    """ContextBindingPort should define bind_context entrypoint."""
    assert hasattr(adapter_ports.ContextBindingPort, "bind_context")


def test_checkpoint_resume_port_protocol_exposes_export_and_import_methods() -> None:
    """CheckpointResumePort should define export_checkpoint/import_resume methods."""
    for method_name in ("export_checkpoint", "import_resume"):
        assert hasattr(adapter_ports.CheckpointResumePort, method_name), method_name


def test_capability_adapter_protocol_exposes_all_resolution_methods() -> None:
    """CapabilityAdapter should define tool/mcp/skill/bundle resolution entrypoints."""
    for method_name in (
        "resolve_tool_bindings",
        "resolve_mcp_bindings",
        "resolve_skill_bindings",
        "resolve_declarative_bundle",
    ):
        assert hasattr(adapter_ports.CapabilityAdapter, method_name), method_name
