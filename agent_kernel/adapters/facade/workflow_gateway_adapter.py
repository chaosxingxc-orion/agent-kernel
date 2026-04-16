"""Compatibility adapter for workflow gateway signal APIs.

This module keeps legacy ``signal_run(request)`` compatibility out of
``KernelFacade`` so facade code can consistently call the protocol method
``signal_workflow(run_id, request)``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import SignalRunRequest, TemporalWorkflowGateway


class WorkflowGatewaySignalAdapter:
    """Proxy that normalizes gateway signal method shape.

    Preferred API:
      - ``signal_workflow(run_id, request)``
    Legacy compatibility API:
      - ``signal_run(request)``
    """

    def __init__(self, gateway: Any) -> None:
        """Initializes WorkflowGatewaySignalAdapter."""
        self._gateway = gateway

    async def signal_workflow(self, run_id: str, request: SignalRunRequest) -> None:
        """Route signals through protocol or legacy gateway method."""
        signal_workflow = getattr(self._gateway, "signal_workflow", None)
        if callable(signal_workflow):
            maybe_result = signal_workflow(run_id, request)
            if inspect.isawaitable(maybe_result):
                await maybe_result
                return

        signal_run = getattr(self._gateway, "signal_run", None)
        if callable(signal_run):
            maybe_result = signal_run(request)
            if inspect.isawaitable(maybe_result):
                await maybe_result
            return

        raise RuntimeError(
            "workflow_gateway must provide signal_workflow(run_id, request) or signal_run(request)."
        )

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped gateway."""
        return getattr(self._gateway, name)


def adapt_workflow_gateway(gateway: Any) -> TemporalWorkflowGateway:
    """Return a signal-compatible gateway proxy."""
    return cast("TemporalWorkflowGateway", WorkflowGatewaySignalAdapter(gateway))
