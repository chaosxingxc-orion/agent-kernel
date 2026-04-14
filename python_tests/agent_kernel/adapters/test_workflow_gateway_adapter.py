"""Tests for facade workflow gateway signal compatibility adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.workflow_gateway_adapter import adapt_workflow_gateway
from agent_kernel.kernel.contracts import SignalRunRequest


def _signal_request() -> SignalRunRequest:
    return SignalRunRequest(
        run_id="run-1",
        signal_type="tool_result",
        signal_payload={"ok": True},
        caused_by="test",
    )


def test_adapter_prefers_signal_workflow_when_available() -> None:
    gateway = MagicMock()
    gateway.signal_workflow = AsyncMock()
    gateway.signal_run = AsyncMock()
    adapted = adapt_workflow_gateway(gateway)

    asyncio.run(adapted.signal_workflow("run-1", _signal_request()))

    gateway.signal_workflow.assert_awaited_once()
    gateway.signal_run.assert_not_called()


def test_adapter_falls_back_to_signal_run_when_signal_workflow_not_awaitable() -> None:
    gateway = MagicMock()
    # MagicMock default call returns non-awaitable sentinel.
    gateway.signal_workflow = MagicMock()
    gateway.signal_run = AsyncMock()
    adapted = adapt_workflow_gateway(gateway)

    asyncio.run(adapted.signal_workflow("run-1", _signal_request()))

    gateway.signal_workflow.assert_called_once()
    gateway.signal_run.assert_awaited_once()


def test_adapter_raises_when_gateway_has_no_compatible_signal_api() -> None:
    gateway = MagicMock()
    del gateway.signal_workflow
    del gateway.signal_run
    adapted = adapt_workflow_gateway(gateway)

    with pytest.raises(RuntimeError, match="signal_workflow"):
        asyncio.run(adapted.signal_workflow("run-1", _signal_request()))
