"""Verifies for execute inference and execute skill script in temporalsdkactivitygateway."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_kernel.kernel.contracts import (
    AdmissionResult,
    ContextWindow,
    InferenceActivityInput,
    InferenceConfig,
    ModelOutput,
    ScriptActivityInput,
    ScriptResult,
)
from agent_kernel.substrate.temporal.activity_gateway import (
    TemporalActivityBindings,
    TemporalSDKActivityGateway,
)


def _bindings_no_cognitive() -> TemporalActivityBindings:
    """Bindings no cognitive."""
    return TemporalActivityBindings(
        admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
        tool_activity=lambda _: None,
        mcp_activity=lambda _: None,
        verification_activity=lambda _: {},
        reconciliation_activity=lambda _: {},
    )


def _make_context_window() -> ContextWindow:
    """Make context window."""
    return ContextWindow(system_instructions="test")


def _make_inference_input() -> InferenceActivityInput:
    """Make inference input."""
    return InferenceActivityInput(
        run_id="r-1",
        turn_id="t-1",
        context_window=_make_context_window(),
        config=InferenceConfig(model_ref="echo"),
        idempotency_key=uuid.uuid4().hex,
    )


def _make_script_input() -> ScriptActivityInput:
    """Make script input."""
    return ScriptActivityInput(
        run_id="r-1",
        action_id="a-1",
        script_id="s-1",
        script_content="print('hello')",
        host_kind="in_process_python",
    )


class TestExecuteInferenceOnGateway:
    """Test suite for ExecuteInferenceOnGateway."""

    def test_raises_when_no_inference_callable_registered(self) -> None:
        """Verifies raises when no inference callable registered."""
        gw = TemporalSDKActivityGateway(_bindings_no_cognitive())
        with pytest.raises(RuntimeError, match="inference_activity"):
            asyncio.run(gw.execute_inference(_make_inference_input()))

    def test_delegates_to_inference_callable(self) -> None:
        """Verifies delegates to inference callable."""
        expected = ModelOutput(raw_text="hello", finish_reason="stop")

        async def _inference(_req: InferenceActivityInput) -> ModelOutput:
            """Inference."""
            return expected

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            inference_activity=_inference,
        )
        gw = TemporalSDKActivityGateway(bindings)
        result = asyncio.run(gw.execute_inference(_make_inference_input()))
        assert result is expected

    def test_sync_callable_also_works(self) -> None:
        """Verifies sync callable also works."""
        expected = ModelOutput(raw_text="sync-result")

        def _sync_inference(_req: InferenceActivityInput) -> ModelOutput:
            """Sync inference."""
            return expected

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            inference_activity=_sync_inference,
        )
        gw = TemporalSDKActivityGateway(bindings)
        result = asyncio.run(gw.execute_inference(_make_inference_input()))
        assert result is expected

    def test_run_id_forwarded_to_callable(self) -> None:
        """Verifies run id forwarded to callable."""
        received: list[str] = []

        async def _inference(req: InferenceActivityInput) -> ModelOutput:
            """Inference."""
            received.append(req.run_id)
            return ModelOutput(raw_text="")

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            inference_activity=_inference,
        )
        gw = TemporalSDKActivityGateway(bindings)
        inp = _make_inference_input()
        asyncio.run(gw.execute_inference(inp))
        assert received == ["r-1"]


class TestExecuteSkillScriptOnGateway:
    """Test suite for ExecuteSkillScriptOnGateway."""

    def test_raises_when_no_script_callable_registered(self) -> None:
        """Verifies raises when no script callable registered."""
        gw = TemporalSDKActivityGateway(_bindings_no_cognitive())
        with pytest.raises(RuntimeError, match="script_activity"):
            asyncio.run(gw.execute_skill_script(_make_script_input()))

    def test_delegates_to_script_callable(self) -> None:
        """Verifies delegates to script callable."""
        expected = ScriptResult(script_id="s-1", exit_code=0, stdout="hello")

        async def _script(_req: ScriptActivityInput) -> ScriptResult:
            """Script."""
            return expected

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            script_activity=_script,
        )
        gw = TemporalSDKActivityGateway(bindings)
        result = asyncio.run(gw.execute_skill_script(_make_script_input()))
        assert result is expected

    def test_script_id_forwarded(self) -> None:
        """Verifies script id forwarded."""
        received: list[str] = []

        async def _script(req: ScriptActivityInput) -> ScriptResult:
            """Script."""
            received.append(req.script_id)
            return ScriptResult(script_id=req.script_id, exit_code=0)

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            script_activity=_script,
        )
        gw = TemporalSDKActivityGateway(bindings)
        asyncio.run(gw.execute_skill_script(_make_script_input()))
        assert received == ["s-1"]

    def test_both_cognitive_callables_can_coexist(self) -> None:
        """Verifies both cognitive callables can coexist."""
        inf_called: list[bool] = []
        scr_called: list[bool] = []

        async def _inf(_: InferenceActivityInput) -> ModelOutput:
            """Inf."""
            inf_called.append(True)
            return ModelOutput(raw_text="")

        async def _scr(_: ScriptActivityInput) -> ScriptResult:
            """Scr."""
            scr_called.append(True)
            return ScriptResult(script_id="x", exit_code=0)

        bindings = TemporalActivityBindings(
            admission_activity=lambda _: AdmissionResult(admitted=True, reason_code="ok"),
            tool_activity=lambda _: None,
            mcp_activity=lambda _: None,
            verification_activity=lambda _: {},
            reconciliation_activity=lambda _: {},
            inference_activity=_inf,
            script_activity=_scr,
        )
        gw = TemporalSDKActivityGateway(bindings)
        asyncio.run(gw.execute_inference(_make_inference_input()))
        asyncio.run(gw.execute_skill_script(_make_script_input()))
        assert inf_called == [True]
        assert scr_called == [True]
