"""Tests for Temporal activity gateway callable-based adapter behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_kernel.kernel.contracts import (
    Action,
    AdmissionActivityInput,
    AdmissionResult,
    EffectClass,
    MCPActivityInput,
    ReconciliationActivityInput,
    RunProjection,
    ToolActivityInput,
    VerificationActivityInput,
)
from agent_kernel.substrate.temporal.activity_gateway import (
    ActivityHandlerNotRegisteredError,
    TemporalActivityBindings,
    TemporalSDKActivityGateway,
)


@dataclass(slots=True)
class _CallRecorder:
    """Captures ordered activity calls for gateway forwarding assertions."""

    calls: list[tuple[str, Any]] = field(default_factory=list)


def _make_action() -> Action:
    """Builds one minimal valid Action contract fixture for tests."""

    return Action(
        action_id="action-1",
        run_id="run-1",
        action_type="tool",
        effect_class=EffectClass.READ_ONLY,
    )


def _make_projection() -> RunProjection:
    """Builds one minimal valid RunProjection fixture for tests."""

    return RunProjection(
        run_id="run-1",
        lifecycle_state="ready",
        projected_offset=10,
        waiting_external=False,
        ready_for_dispatch=True,
    )


def test_activity_gateway_executes_registered_handlers() -> None:
    """Gateway should execute tool and MCP handlers only from explicit registry."""

    recorder = _CallRecorder()

    async def admission_activity(
        payload: AdmissionActivityInput,
    ) -> AdmissionResult:
        recorder.calls.append(("admission", payload))
        return AdmissionResult(admitted=True, reason_code="ok")

    async def tool_activity(payload: ToolActivityInput) -> dict[str, Any]:
        recorder.calls.append(("tool", payload))
        return {"tool": payload.tool_name, "ok": True}

    async def mcp_activity(payload: MCPActivityInput) -> dict[str, Any]:
        recorder.calls.append(("mcp", payload))
        return {"server": payload.server_name, "operation": payload.operation}

    async def verification_activity(
        payload: VerificationActivityInput,
    ) -> dict[str, Any]:
        recorder.calls.append(("verification", payload))
        return {"kind": payload.verification_kind, "passed": True}

    async def reconciliation_activity(
        payload: ReconciliationActivityInput,
    ) -> dict[str, Any]:
        recorder.calls.append(("reconciliation", payload))
        return {"reconciled": payload.expected_state == payload.observed_state}

    gateway = TemporalSDKActivityGateway(
        TemporalActivityBindings(
            admission_activity=admission_activity,
            tool_activity=lambda _payload: None,
            mcp_activity=lambda _payload: None,
            verification_activity=verification_activity,
            reconciliation_activity=reconciliation_activity,
        ),
        tool_handlers={"web.search": tool_activity},
        mcp_handlers={("docs", "fetch"): mcp_activity},
    )

    admission_result = asyncio.run(
        gateway.execute_admission(
            AdmissionActivityInput(
                run_id="run-1",
                action=_make_action(),
                projection=_make_projection(),
            )
        )
    )
    tool_result = asyncio.run(
        gateway.execute_tool(
            ToolActivityInput(
                run_id="run-1",
                action_id="action-1",
                tool_name="web.search",
                arguments={"q": "kernel contract"},
            )
        )
    )
    mcp_result = asyncio.run(
        gateway.execute_mcp(
            MCPActivityInput(
                run_id="run-1",
                action_id="action-1",
                server_name="docs",
                operation="fetch",
                arguments={"path": "README.md"},
            )
        )
    )
    verification_result = asyncio.run(
        gateway.execute_verification(
            VerificationActivityInput(
                run_id="run-1",
                action_id="action-1",
                verification_kind="post_condition",
                evidence={"status": "ok"},
            )
        )
    )
    reconciliation_result = asyncio.run(
        gateway.execute_reconciliation(
            ReconciliationActivityInput(
                run_id="run-1",
                action_id="action-1",
                expected_state={"status": "ok"},
                observed_state={"status": "ok"},
            )
        )
    )

    assert admission_result.admitted is True
    assert tool_result == {"tool": "web.search", "ok": True}
    assert mcp_result == {"server": "docs", "operation": "fetch"}
    assert verification_result == {"kind": "post_condition", "passed": True}
    assert reconciliation_result == {"reconciled": True}
    assert [call_name for call_name, _ in recorder.calls] == [
        "admission",
        "tool",
        "mcp",
        "verification",
        "reconciliation",
    ]


def test_activity_gateway_raises_for_unregistered_tool_handler() -> None:
    """Gateway should fail fast when a tool handler was not registered."""

    gateway = TemporalSDKActivityGateway(
        TemporalActivityBindings(
            admission_activity=lambda _payload: AdmissionResult(
                admitted=False,
                reason_code="policy_denied",
            ),
            tool_activity=lambda _payload: None,
            mcp_activity=lambda _payload: None,
            verification_activity=lambda payload: ({"verified": payload.verification_kind}),
            reconciliation_activity=lambda payload: ({"expected": payload.expected_state}),
        )
    )

    with pytest.raises(
        ActivityHandlerNotRegisteredError,
        match=r"missing\.tool",
    ):
        asyncio.run(
            gateway.execute_tool(
                ToolActivityInput(
                    run_id="run-1",
                    action_id="action-1",
                    tool_name="missing.tool",
                )
            )
        )


def test_activity_gateway_raises_for_unregistered_mcp_handler() -> None:
    """Gateway should fail fast when an MCP handler was not registered."""

    gateway = TemporalSDKActivityGateway(
        TemporalActivityBindings(
            admission_activity=lambda _payload: AdmissionResult(
                admitted=False,
                reason_code="policy_denied",
            ),
            tool_activity=lambda _payload: None,
            mcp_activity=lambda _payload: None,
            verification_activity=lambda payload: ({"verified": payload.verification_kind}),
            reconciliation_activity=lambda payload: ({"expected": payload.expected_state}),
        )
    )

    with pytest.raises(
        ActivityHandlerNotRegisteredError,
        match="docs/fetch",
    ):
        asyncio.run(
            gateway.execute_mcp(
                MCPActivityInput(
                    run_id="run-1",
                    action_id="action-1",
                    server_name="docs",
                    operation="fetch",
                    arguments={"path": "README.md"},
                )
            )
        )
