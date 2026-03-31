"""Tests adapter alignment with openjiuwen-style runner/session objects."""

from __future__ import annotations

import asyncio

from agent_kernel.adapters.agent_core.runner_adapter import AgentCoreRunnerAdapter
from agent_kernel.adapters.agent_core.session_adapter import (
    AgentCoreCallbackInput,
    AgentCoreSessionAdapter,
)


class _FakeWorkflowSession:
    def __init__(self, session_id: str, workflow_id: str) -> None:
        self._session_id = session_id
        self._workflow_id = workflow_id

    def session_id(self) -> str:
        return self._session_id

    def workflow_id(self) -> str:
        return self._workflow_id


def test_runner_adapter_maps_openjiuwen_run_call() -> None:
    adapter = AgentCoreRunnerAdapter()
    session = _FakeWorkflowSession("session-42", "wf-42")

    request = adapter.from_openjiuwen_run_call(
        runner_kind="workflow:research",
        inputs={"query": "kernel"},
        session=session,
        context_ref="ctx-42",
    )

    assert request.initiator == "agent_core_runner"
    assert request.run_kind == "workflow:research"
    assert request.session_id == "session-42"
    assert request.input_json == {"query": "kernel"}
    assert request.context_ref == "ctx-42"


def test_runner_adapter_prefers_workflow_id_for_child_calls() -> None:
    adapter = AgentCoreRunnerAdapter()
    session = _FakeWorkflowSession("session-99", "wf-parent")

    request = adapter.from_openjiuwen_child_run_call(
        runner_kind="workflow:verification",
        child_inputs={"artifact": "report"},
        parent_session=session,
    )

    assert request.parent_run_id == "wf-parent"
    assert request.child_kind == "workflow:verification"
    assert request.input_json == {"artifact": "report"}


def test_session_adapter_routes_callback_to_latest_bound_run() -> None:
    adapter = AgentCoreSessionAdapter()
    session = _FakeWorkflowSession("session-1", "wf-1")

    asyncio.run(adapter.bind_openjiuwen_session(session, "run-a"))
    asyncio.run(adapter.bind_openjiuwen_session(session, "run-b", "child"))

    signal = adapter.translate_callback(
        AgentCoreCallbackInput(
            session_id="session-1",
            callback_type="tool_result",
            callback_payload={"ok": True},
            caused_by="cb-1",
        )
    )

    assert signal.run_id == "run-b"
    assert signal.signal_type == "tool_result"
    assert signal.signal_payload == {"ok": True}
    assert signal.caused_by == "cb-1"
