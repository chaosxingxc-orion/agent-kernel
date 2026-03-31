"""Kernel-level adapter port protocols for v6.4 boundary abstraction."""

from __future__ import annotations

from typing import Any, Protocol

from agent_kernel.adapters.agent_core.context_adapter import RuntimeContextBinding
from agent_kernel.kernel.contracts import (
    ResumeRunRequest,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
)


class IngressAdapter(Protocol):
    """Unifies platform ingress translation into kernel run/signal contracts."""

    def from_runner_start(self, input_value: Any) -> StartRunRequest:
        """Translates runner start payload into StartRunRequest."""
        ...

    def from_session_signal(self, input_value: Any) -> SignalRunRequest:
        """Translates session signal/callback payload into SignalRunRequest."""
        ...

    def from_callback(self, input_value: Any) -> SignalRunRequest:
        """Translates callback payload into SignalRunRequest."""
        ...


class ContextBindingPort(Protocol):
    """Maps external context payloads to normalized runtime context bindings."""

    def bind_context(self, input_value: Any) -> RuntimeContextBinding:
        """Binds context for run execution and returns binding metadata."""
        ...


class CheckpointResumePort(Protocol):
    """Owns checkpoint export and resume import boundary mapping."""

    async def export_checkpoint(self, run_id: str) -> Any:
        """Exports checkpoint view for platform consumption."""
        ...

    async def import_resume(self, input_value: Any) -> ResumeRunRequest:
        """Imports platform resume payload into kernel resume contract."""
        ...


class CapabilityAdapter(Protocol):
    """Resolves capability bindings from platform metadata/payloads."""

    async def resolve_tool_bindings(self, action: Any, metadata: Any | None = None) -> Any:
        """Resolves tool bindings."""
        ...

    async def resolve_mcp_bindings(self, action: Any, metadata: Any | None = None) -> Any:
        """Resolves MCP bindings."""
        ...

    async def resolve_skill_bindings(self, action: Any, metadata: Any | None = None) -> Any:
        """Resolves skill bindings."""
        ...

    async def resolve_declarative_bundle(self, action: Any) -> Any:
        """Resolves declarative bundle digest or reference."""
        ...


class ChildRunIngressPort(Protocol):
    """Optional ingress port for child-run spawn translation."""

    def from_runner_child_spawn(self, input_value: Any) -> SpawnChildRunRequest:
        """Translates child-run spawn payload into SpawnChildRunRequest."""
        ...

