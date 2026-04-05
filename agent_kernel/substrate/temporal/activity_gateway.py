"""Temporal activity gateway adapter for kernel-safe activity invocation.

Design constraints:
  - The adapter must not encode business truth.
  - Activity selection comes from injected callables.
  - Input and output stay on kernel DTO contracts.

This implementation intentionally avoids any dependency on a real
Temporal worker process. Tests can inject plain Python callables
to validate behavior.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_kernel.kernel.contracts import (
    AdmissionActivityInput,
    AdmissionResult,
    InferenceActivityInput,
    MCPActivityInput,
    ModelOutput,
    ReconciliationActivityInput,
    ScriptActivityInput,
    ScriptResult,
    TemporalActivityGateway,
    ToolActivityInput,
    VerificationActivityInput,
)

AdmissionActivityCallable = Callable[
    [AdmissionActivityInput],
    AdmissionResult | Awaitable[AdmissionResult],
]
ToolActivityCallable = Callable[
    [ToolActivityInput],
    Any | Awaitable[Any],
]
MCPActivityCallable = Callable[
    [MCPActivityInput],
    Any | Awaitable[Any],
]
VerificationActivityCallable = Callable[
    [VerificationActivityInput],
    Any | Awaitable[Any],
]
ReconciliationActivityCallable = Callable[
    [ReconciliationActivityInput],
    Any | Awaitable[Any],
]
MCPHandlerKey = tuple[str, str]
InferenceActivityCallable = Callable[
    [InferenceActivityInput],
    ModelOutput | Awaitable[ModelOutput],
]
ScriptActivityCallable = Callable[
    [ScriptActivityInput],
    ScriptResult | Awaitable[ScriptResult],
]


class ActivityHandlerNotRegisteredError(LookupError):
    """Raised when tool or MCP activity execution has no registered handler."""

    def __init__(
        self,
        *,
        route: str,
        identifier: str,
    ) -> None:
        """Initialize the error with route and identifier.

        Args:
            route: Activity route type (e.g. "tool" or "mcp").
            identifier: Handler identifier that was not found.

        """
        super().__init__(
            f"No registered {route} activity handler for"
            f" '{identifier}'. Register an explicit handler"
            " before execution."
        )


@dataclass(frozen=True, slots=True)
class TemporalActivityBindings:
    """Holds callable bindings for each Temporal activity surface.

    Attributes:
        admission_activity: Callable that executes admission policy
            checks.
        tool_activity: Callable that executes tool side effects.
        mcp_activity: Callable that executes MCP server operations.
        verification_activity: Callable that validates execution
            outcomes.
        reconciliation_activity: Callable that reconciles state
            divergence.

    """

    admission_activity: AdmissionActivityCallable
    tool_activity: ToolActivityCallable
    mcp_activity: MCPActivityCallable
    verification_activity: VerificationActivityCallable
    reconciliation_activity: ReconciliationActivityCallable
    inference_activity: InferenceActivityCallable | None = None
    script_activity: ScriptActivityCallable | None = None


class TemporalSDKActivityGateway(TemporalActivityGateway):
    """Executes activity callables through Temporal gateway contract.

    This class only translates protocol method calls into callable
    invocation. It does not interpret payloads, enforce policies, or
    mutate kernel truth.
    """

    def __init__(
        self,
        bindings: TemporalActivityBindings,
        *,
        tool_handlers: (Mapping[str, ToolActivityCallable] | None) = None,
        mcp_handlers: (Mapping[MCPHandlerKey, MCPActivityCallable] | None) = None,
    ) -> None:
        """Initialize the gateway with bindings and explicit handlers.

        Args:
            bindings: Default activity callable bindings.
            tool_handlers: Optional explicit tool handler map.
            mcp_handlers: Optional explicit MCP handler map.

        """
        self._bindings = bindings
        self._tool_handlers: dict[str, ToolActivityCallable] = dict(tool_handlers or {})
        self._mcp_handlers: dict[MCPHandlerKey, MCPActivityCallable] = dict(mcp_handlers or {})

    def register_tool_handler(
        self,
        tool_name: str,
        handler: ToolActivityCallable,
    ) -> None:
        """Register one tool handler by ``tool_name``.

        Args:
            tool_name: Tool identifier for handler registration.
            handler: Callable to execute when tool is invoked.

        """
        self._tool_handlers[tool_name] = handler

    def get_tool_handler(
        self,
        tool_name: str,
    ) -> ToolActivityCallable:
        """Get one registered tool handler by ``tool_name``.

        Args:
            tool_name: Tool identifier to look up.

        Returns:
            The registered tool handler callable.

        Raises:
            ActivityHandlerNotRegisteredError: If the tool handler
                is missing.

        """
        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            raise ActivityHandlerNotRegisteredError(
                route="tool",
                identifier=tool_name,
            )
        return handler

    def register_mcp_handler(
        self,
        server_name: str,
        capability: str,
        handler: MCPActivityCallable,
    ) -> None:
        """Register one MCP handler by ``server_name/capability``.

        Args:
            server_name: MCP server name.
            capability: MCP capability identifier.
            handler: Callable to execute when MCP is invoked.

        """
        self._mcp_handlers[(server_name, capability)] = handler

    def get_mcp_handler(
        self,
        server_name: str,
        capability: str,
    ) -> MCPActivityCallable:
        """Get one registered MCP handler by ``server_name/capability``.

        Args:
            server_name: MCP server name.
            capability: MCP capability identifier.

        Returns:
            The registered MCP handler callable.

        Raises:
            ActivityHandlerNotRegisteredError: If the MCP handler
                is missing.

        """
        key = (server_name, capability)
        handler = self._mcp_handlers.get(key)
        if handler is None:
            raise ActivityHandlerNotRegisteredError(
                route="mcp",
                identifier=f"{server_name}/{capability}",
            )
        return handler

    async def execute_admission(
        self,
        request: AdmissionActivityInput,
    ) -> AdmissionResult:
        """Execute the injected admission activity callable.

        Args:
            request: Admission activity input payload.

        Returns:
            Admission decision result from the activity.

        """
        return await self._invoke(
            self._bindings.admission_activity,
            request,
        )

    async def execute_tool(
        self,
        request: ToolActivityInput,
    ) -> Any:
        """Execute one explicitly registered tool handler.

        Args:
            request: Tool activity input payload.

        Returns:
            Tool execution result from the handler.

        """
        return await self._invoke(
            self.get_tool_handler(request.tool_name),
            request,
        )

    async def execute_mcp(
        self,
        request: MCPActivityInput,
    ) -> Any:
        """Execute one explicitly registered MCP handler.

        Args:
            request: MCP activity input payload.

        Returns:
            MCP execution result from the handler.

        """
        return await self._invoke(
            self.get_mcp_handler(
                request.server_name,
                request.operation,
            ),
            request,
        )

    async def execute_verification(
        self,
        request: VerificationActivityInput,
    ) -> Any:
        """Execute the injected verification activity callable.

        Args:
            request: Verification activity input payload.

        Returns:
            Verification result from the activity.

        """
        return await self._invoke(
            self._bindings.verification_activity,
            request,
        )

    async def execute_reconciliation(
        self,
        request: ReconciliationActivityInput,
    ) -> Any:
        """Execute the injected reconciliation activity callable.

        Args:
            request: Reconciliation activity input payload.

        Returns:
            Reconciliation result from the activity.

        """
        return await self._invoke(
            self._bindings.reconciliation_activity,
            request,
        )

    async def execute_inference(
        self,
        request: InferenceActivityInput,
    ) -> ModelOutput:
        """Execute the injected inference activity callable.

        Args:
            request: Inference activity input payload.

        Returns:
            Normalised model output.

        Raises:
            RuntimeError: If no inference_activity callable is registered.

        """
        if self._bindings.inference_activity is None:
            raise RuntimeError(
                "No inference_activity callable registered in TemporalActivityBindings."
            )
        return await self._invoke(self._bindings.inference_activity, request)

    async def execute_skill_script(
        self,
        request: ScriptActivityInput,
    ) -> ScriptResult:
        """Execute the injected script activity callable.

        Args:
            request: Script activity input payload.

        Returns:
            Script execution result.

        Raises:
            RuntimeError: If no script_activity callable is registered.

        """
        if self._bindings.script_activity is None:
            raise RuntimeError(
                "No script_activity callable registered in TemporalActivityBindings."
            )
        return await self._invoke(self._bindings.script_activity, request)

    async def _invoke(
        self,
        activity_callable: Callable[[Any], Any],
        request: Any,
    ) -> Any:
        """Invoke one activity callable and awaits awaitable results.

        Args:
            activity_callable: Injected activity function for one
                contract method.
            request: Contract DTO forwarded to the callable.

        Returns:
            The callable result, preserving payload type from the
            activity.

        """
        result = activity_callable(request)
        if inspect.isawaitable(result):
            return await result
        return result
