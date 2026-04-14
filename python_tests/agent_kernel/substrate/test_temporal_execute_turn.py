"""Tests for TemporalSDKWorkflowGateway.execute_turn() routing.

Uses MagicMock for the Temporal client (external network isolation) and
AsyncMock for activity_gateway (external network isolation).
Legitimate mock use: preventing real Temporal SDK network calls in unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.contracts import MCPActivityInput, ToolActivityInput
from agent_kernel.substrate.temporal.gateway import TemporalSDKWorkflowGateway


def _make_gateway(activity_gateway=None) -> TemporalSDKWorkflowGateway:
    """Build a gateway with a mock Temporal client."""
    client = MagicMock()
    return TemporalSDKWorkflowGateway(
        temporal_client=client,
        activity_gateway=activity_gateway,
    )


class TestExecuteTurn:
    """Unit tests for execute_turn action routing."""

    @pytest.mark.asyncio
    async def test_no_gateway_raises_runtime_error(self) -> None:
        """execute_turn must raise RuntimeError when no activity_gateway is set."""
        gw = _make_gateway(activity_gateway=None)
        action = MagicMock()
        action.action_type = "tool_call"
        with pytest.raises(RuntimeError, match="activity_gateway"):
            await gw.execute_turn("run-1", action, None, idempotency_key="k1")

    @pytest.mark.asyncio
    async def test_tool_call_routes_to_execute_tool(self) -> None:
        """tool_call action_type must call activity_gateway.execute_tool."""
        ag = AsyncMock()
        ag.execute_tool = AsyncMock(return_value={"result": "ok"})
        gw = _make_gateway(activity_gateway=ag)

        action = MagicMock()
        action.action_type = "tool_call"
        action.tool_name = "my_tool"
        action.arguments = {"x": 1}

        result = await gw.execute_turn("run-1", action, None, idempotency_key="idem-k1")

        ag.execute_tool.assert_called_once()
        call_arg = ag.execute_tool.call_args[0][0]
        assert isinstance(call_arg, ToolActivityInput)
        assert call_arg.run_id == "run-1"
        assert call_arg.action_id == "idem-k1"
        assert call_arg.tool_name == "my_tool"
        assert call_arg.arguments == {"x": 1}
        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_mcp_call_routes_to_execute_mcp(self) -> None:
        """mcp_call action_type must call activity_gateway.execute_mcp."""
        ag = AsyncMock()
        ag.execute_mcp = AsyncMock(return_value={"mcp": "done"})
        gw = _make_gateway(activity_gateway=ag)

        action = MagicMock()
        action.action_type = "mcp_call"
        action.mcp_server = "my_server"
        action.mcp_capability = "do_thing"
        action.arguments = {"y": 2}

        result = await gw.execute_turn("run-2", action, None, idempotency_key="idem-k2")

        ag.execute_mcp.assert_called_once()
        call_arg = ag.execute_mcp.call_args[0][0]
        assert isinstance(call_arg, MCPActivityInput)
        assert call_arg.run_id == "run-2"
        assert call_arg.action_id == "idem-k2"
        assert call_arg.server_name == "my_server"
        assert call_arg.operation == "do_thing"
        assert call_arg.arguments == {"y": 2}
        assert result == {"mcp": "done"}

    @pytest.mark.asyncio
    async def test_unsupported_type_raises_value_error(self) -> None:
        """Unknown action_type must raise ValueError."""
        ag = AsyncMock()
        gw = _make_gateway(activity_gateway=ag)

        action = MagicMock()
        action.action_type = "unknown_type"

        with pytest.raises(ValueError, match="unsupported action_type"):
            await gw.execute_turn("run-3", action, None, idempotency_key="k3")
