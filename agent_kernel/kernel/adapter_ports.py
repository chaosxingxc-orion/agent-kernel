"""Kernel-level adapter port protocols for v6.4 boundary abstraction."""

from __future__ import annotations

from typing import Any, Protocol

from agent_kernel.kernel.contracts import (
    CapabilityAdapter,
    CheckpointResumePort,
    ContextBindingPort,
    IngressAdapter,
    SpawnChildRunRequest,
)


class ChildRunIngressPort(Protocol):
    """Optional ingress port for child-run spawn translation."""

    def from_runner_child_spawn(self, input_value: Any) -> SpawnChildRunRequest:
        """Translates child-run spawn payload into SpawnChildRunRequest.
        Args:
            input_value:
        Returns:
            SpawnChildRunRequest:
        """
        ...


__all__ = [
    "CapabilityAdapter",
    "CheckpointResumePort",
    "ChildRunIngressPort",
    "ContextBindingPort",
    "IngressAdapter",
]
