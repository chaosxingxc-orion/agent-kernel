"""Temporal integration tests for runtime bundle and worker wiring."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, ClassVar

import pytest

from agent_kernel.adapters.agent_core.session_adapter import AgentCoreCallbackInput
from agent_kernel.kernel.contracts import (
    QueryRunRequest,
    SignalRunRequest,
    StartRunRequest,
)
from agent_kernel.runtime.bundle import AgentKernelRuntimeBundle
from agent_kernel.substrate.temporal.gateway import TemporalGatewayConfig
from agent_kernel.substrate.temporal.worker import TemporalWorkerConfig

try:
    from temporalio import workflow as temporal_workflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner

    TEMPORAL_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency in CI
    temporal_workflow = None
    WorkflowEnvironment = None
    UnsandboxedWorkflowRunner = None
    TEMPORAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not TEMPORAL_AVAILABLE,
    reason="temporalio SDK is not installed in this environment.",
)


class _Session:
    """Minimal session object for orchestrator start path."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def session_id(self) -> str:
        return self._session_id


if TEMPORAL_AVAILABLE:

    @temporal_workflow.defn
    class _BundleIntegrationWorkflow:
        """Workflow used to validate bundle-created worker and query mapping."""

        def __init__(self) -> None:
            self._projection = {
                "run_id": "unknown",
                "lifecycle_state": "ready",
                "projected_offset": 0,
                "waiting_external": True,
                "ready_for_dispatch": True,
                "recovery_mode": "human_escalation",
                "recovery_reason": "waiting_callback",
                "active_child_runs": [],
            }

        @temporal_workflow.run
        async def run(self, run_input: Any) -> str:
            if isinstance(run_input, dict):
                self._projection["run_id"] = str(run_input.get("run_id", "run-x"))
            else:
                self._projection["run_id"] = str(getattr(run_input, "run_id", "run-x"))
            await temporal_workflow.wait_condition(
                lambda: self._projection["projected_offset"] >= 1
            )
            return "done"

        @temporal_workflow.signal
        async def signal(self, payload: dict[str, Any]) -> None:
            signal_type = str(payload.get("signal_type", "unknown"))
            if signal_type == "tool_result":
                self._projection["projected_offset"] += 1
                self._projection["waiting_external"] = False
                self._projection["recovery_mode"] = None
                self._projection["recovery_reason"] = None

        @temporal_workflow.query
        def query(self) -> dict[str, Any]:
            return self._projection

    @temporal_workflow.defn
    class _OperationalPrecedenceWorkflow:
        """Workflow fixture for operational precedence integration coverage."""

        _PRIORITY_RANK: ClassVar[dict[str, int]] = {
            "external_callback": 1,
            "timeout": 2,
            "hard_failure": 3,
            "cancel_requested": 4,
        }

        def __init__(self) -> None:
            self._projection = {
                "run_id": "unknown",
                "lifecycle_state": "created",
                "projected_offset": 0,
                "waiting_external": False,
                "ready_for_dispatch": False,
                "recovery_mode": None,
                "recovery_reason": None,
                "active_child_runs": [],
            }
            self._seen_signal_tokens: set[str] = set()

        @temporal_workflow.run
        async def run(self, run_input: Any) -> str:
            expected_signals = 1
            if isinstance(run_input, dict):
                self._projection["run_id"] = str(run_input.get("run_id", "run-x"))
                input_json = run_input.get("input_json") or {}
                if isinstance(input_json, dict):
                    raw_expected_signals = input_json.get("expected_signals", 1)
                else:
                    raw_expected_signals = run_input.get("expected_signals", 1)
            else:
                self._projection["run_id"] = str(getattr(run_input, "run_id", "run-x"))
                input_json = getattr(run_input, "input_json", None) or {}
                raw_expected_signals = input_json.get("expected_signals", 1)
            try:
                expected_signals = max(1, int(raw_expected_signals))
            except (TypeError, ValueError):
                expected_signals = 1

            await temporal_workflow.wait_condition(
                lambda: self._projection["projected_offset"] >= expected_signals
            )
            return "done"

        def _projection_priority(self) -> str | None:
            if self._projection["lifecycle_state"] == "aborted":
                reason = str(self._projection.get("recovery_reason") or "")
                if "cancel" in reason:
                    return "cancel_requested"
                return "hard_failure"
            if self._projection["lifecycle_state"] == "waiting_external":
                return "timeout"
            return None

        def _signal_priority(self, signal_type: str) -> str | None:
            if signal_type in self._PRIORITY_RANK:
                return signal_type
            if signal_type in ("callback", "tool_result"):
                return "external_callback"
            return None

        @temporal_workflow.signal
        async def signal(self, payload: dict[str, Any]) -> None:
            signal_type = str(payload.get("signal_type", "unknown"))
            signal_payload = payload.get("signal_payload") or {}
            caused_by = payload.get("caused_by")
            if caused_by:
                signal_token = f"{signal_type}:{caused_by}"
                if signal_token in self._seen_signal_tokens:
                    return
                self._seen_signal_tokens.add(signal_token)

            self._projection["projected_offset"] += 1

            current_priority = self._projection_priority()
            incoming_priority = self._signal_priority(signal_type)
            if (
                current_priority is not None
                and incoming_priority is not None
                and self._PRIORITY_RANK[incoming_priority]
                < self._PRIORITY_RANK[current_priority]
            ):
                return

            if incoming_priority == "cancel_requested":
                self._projection["lifecycle_state"] = "aborted"
                self._projection["waiting_external"] = False
                self._projection["ready_for_dispatch"] = False
                self._projection["recovery_mode"] = "abort"
                self._projection["recovery_reason"] = str(
                    signal_payload.get("reason", "cancel_requested")
                )
                return

            if incoming_priority == "hard_failure":
                self._projection["lifecycle_state"] = "aborted"
                self._projection["waiting_external"] = False
                self._projection["ready_for_dispatch"] = False
                self._projection["recovery_mode"] = "abort"
                self._projection["recovery_reason"] = str(
                    signal_payload.get("reason", "hard_failure")
                )
                return

            if incoming_priority == "timeout":
                self._projection["lifecycle_state"] = "waiting_external"
                self._projection["waiting_external"] = True
                self._projection["ready_for_dispatch"] = False
                self._projection["recovery_mode"] = "human_escalation"
                self._projection["recovery_reason"] = str(
                    signal_payload.get("reason", "timeout")
                )
                return

            if incoming_priority == "external_callback":
                self._projection["lifecycle_state"] = "ready"
                self._projection["waiting_external"] = False
                self._projection["ready_for_dispatch"] = True
                self._projection["recovery_mode"] = None
                self._projection["recovery_reason"] = None

        @temporal_workflow.query
        def query(self) -> dict[str, Any]:
            return self._projection

else:

    class _BundleIntegrationWorkflow:  # pragma: no cover - fallback for type checkers
        """Fallback class used when temporalio is unavailable."""

    class _OperationalPrecedenceWorkflow:  # pragma: no cover
        """Fallback class used when temporalio is unavailable."""


@pytest.mark.asyncio
async def test_bundle_created_worker_runs_facade_start_signal_query_chain() -> None:
    """Bundle worker should host workflow so facade start/signal/query works end to end."""
    assert WorkflowEnvironment is not None
    assert UnsandboxedWorkflowRunner is not None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=env.client,
            temporal_config=TemporalGatewayConfig(
                task_queue="bundle-worker-int-q",
                workflow_id_prefix="run",
                workflow_run_callable=_BundleIntegrationWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        worker = bundle.create_temporal_worker(
            client=env.client,
            config=TemporalWorkerConfig(
                task_queue="bundle-worker-int-q",
                workflows=[_BundleIntegrationWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
        )

        worker_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.2)
            start_request = bundle.runner_adapter.from_openjiuwen_run_call(
                runner_kind="research",
                inputs={"query": "bundle worker"},
                session=_Session("session-bundle-int-1"),
                context_ref=None,
            )
            started = await bundle.facade.start_run(start_request)
            await bundle.session_adapter.bind_run_to_session(
                session_id="session-bundle-int-1",
                run_id=started.run_id,
                binding_kind="primary",
            )
            signal_request = bundle.session_adapter.translate_callback(
                AgentCoreCallbackInput(
                    session_id="session-bundle-int-1",
                    callback_type="tool_result",
                    callback_payload={"ok": True},
                    caused_by="cb-bundle-int-1",
                )
            )
            await bundle.facade.signal_run(signal_request)
            query = await bundle.facade.query_run(QueryRunRequest(run_id=started.run_id))
            assert query.run_id == started.run_id
            assert query.projected_offset >= 1
            assert query.waiting_external is False
            assert query.recovery_mode is None
            assert query.recovery_reason is None
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


async def _wait_for_projection(
    bundle: AgentKernelRuntimeBundle,
    run_id: str,
    *,
    min_offset: int,
    lifecycle_state: str,
    recovery_reason: str,
) -> None:
    """Waits until workflow projection reaches expected lifecycle snapshot."""
    for _ in range(80):
        projection = await bundle.facade.query_run(QueryRunRequest(run_id=run_id))
        if (
            projection.projected_offset >= min_offset
            and projection.lifecycle_state == lifecycle_state
            and projection.recovery_reason == recovery_reason
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Timed out waiting for expected projection state.")


@pytest.mark.asyncio
async def test_bundle_temporal_precedence_matrix_chain_resolves_to_cancel_and_replay_safe() -> None:
    """Bundle Temporal path should honor precedence and ignore replayed duplicate signal."""
    assert WorkflowEnvironment is not None
    assert UnsandboxedWorkflowRunner is not None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=env.client,
            temporal_config=TemporalGatewayConfig(
                task_queue="bundle-worker-priority-q",
                workflow_id_prefix="run",
                workflow_run_callable=_OperationalPrecedenceWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        worker = bundle.create_temporal_worker(
            client=env.client,
            config=TemporalWorkerConfig(
                task_queue="bundle-worker-priority-q",
                workflows=[_OperationalPrecedenceWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
        )

        worker_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.2)
            started = await bundle.facade.start_run(
                StartRunRequest(
                    initiator="system",
                    run_kind="priority-integration",
                    input_json={
                        "run_id": "run-priority-temporal-bundle-1",
                        "expected_signals": 7,
                    },
                )
            )
            run_id = started.run_id
            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="external_callback",
                    signal_payload={"status": "done"},
                    caused_by="callback-1",
                )
            )
            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="timeout",
                    signal_payload={"reason": "tool_timeout"},
                    caused_by="timeout-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=2,
                lifecycle_state="waiting_external",
                recovery_reason="tool_timeout",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="external_callback",
                    signal_payload={"status": "late_callback"},
                    caused_by="callback-2",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=3,
                lifecycle_state="waiting_external",
                recovery_reason="tool_timeout",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="hard_failure",
                    signal_payload={"reason": "executor_fatal"},
                    caused_by="hard-failure-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=4,
                lifecycle_state="aborted",
                recovery_reason="executor_fatal",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="timeout",
                    signal_payload={"reason": "late_timeout"},
                    caused_by="timeout-2",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=5,
                lifecycle_state="aborted",
                recovery_reason="executor_fatal",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="cancel_requested",
                    signal_payload={"reason": "operator_cancel"},
                    caused_by="cancel-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=6,
                lifecycle_state="aborted",
                recovery_reason="operator_cancel",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="cancel_requested",
                    signal_payload={"reason": "duplicate_cancel"},
                    caused_by="cancel-1",
                )
            )
            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="external_callback",
                    signal_payload={"status": "very_late_callback"},
                    caused_by="callback-3",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=7,
                lifecycle_state="aborted",
                recovery_reason="operator_cancel",
            )

            final_projection = await bundle.facade.query_run(QueryRunRequest(run_id=run_id))
            assert final_projection.projected_offset == 7
            assert final_projection.lifecycle_state == "aborted"
            assert final_projection.recovery_mode == "abort"
            assert final_projection.recovery_reason == "operator_cancel"
            assert final_projection.waiting_external is False
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


@pytest.mark.asyncio
async def test_bundle_temporal_precedence_handles_race_like_signal_ordering() -> None:
    """Concurrent signals should still converge to cancel by precedence matrix."""
    assert WorkflowEnvironment is not None
    assert UnsandboxedWorkflowRunner is not None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=env.client,
            temporal_config=TemporalGatewayConfig(
                task_queue="bundle-worker-priority-race-q",
                workflow_id_prefix="run",
                workflow_run_callable=_OperationalPrecedenceWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        worker = bundle.create_temporal_worker(
            client=env.client,
            config=TemporalWorkerConfig(
                task_queue="bundle-worker-priority-race-q",
                workflows=[_OperationalPrecedenceWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
        )

        worker_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.2)
            started = await bundle.facade.start_run(
                StartRunRequest(
                    initiator="system",
                    run_kind="priority-race-integration",
                    input_json={
                        "run_id": "run-priority-temporal-bundle-race-1",
                        "expected_signals": 5,
                    },
                )
            )
            run_id = started.run_id

            await asyncio.gather(
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="external_callback",
                        signal_payload={"status": "done"},
                        caused_by="race-callback-1",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="timeout",
                        signal_payload={"reason": "race_timeout"},
                        caused_by="race-timeout-1",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="hard_failure",
                        signal_payload={"reason": "race_hard_failure"},
                        caused_by="race-hard-failure-1",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="cancel_requested",
                        signal_payload={"reason": "race_cancel"},
                        caused_by="race-cancel-1",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="cancel_requested",
                        signal_payload={"reason": "race_cancel"},
                        caused_by="race-cancel-1",
                    )
                ),
            )
            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="external_callback",
                    signal_payload={"status": "very_late_race_callback"},
                    caused_by="race-callback-2",
                )
            )

            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=5,
                lifecycle_state="aborted",
                recovery_reason="race_cancel",
            )
            final_projection = await bundle.facade.query_run(QueryRunRequest(run_id=run_id))
            assert final_projection.projected_offset == 5
            assert final_projection.lifecycle_state == "aborted"
            assert final_projection.recovery_mode == "abort"
            assert final_projection.recovery_reason == "race_cancel"
            assert final_projection.waiting_external is False
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


@pytest.mark.asyncio
async def test_bundle_temporal_precedence_replay_race_preserves_cancel_terminal_priority() -> None:
    """Replay duplicates and concurrent late signals must never override cancel terminality."""
    assert WorkflowEnvironment is not None
    assert UnsandboxedWorkflowRunner is not None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=env.client,
            temporal_config=TemporalGatewayConfig(
                task_queue="bundle-worker-priority-race-replay-q",
                workflow_id_prefix="run",
                workflow_run_callable=_OperationalPrecedenceWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        worker = bundle.create_temporal_worker(
            client=env.client,
            config=TemporalWorkerConfig(
                task_queue="bundle-worker-priority-race-replay-q",
                workflows=[_OperationalPrecedenceWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
        )

        worker_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.2)
            started = await bundle.facade.start_run(
                StartRunRequest(
                    initiator="system",
                    run_kind="priority-race-replay-integration",
                    input_json={
                        "run_id": "run-priority-temporal-bundle-race-replay-1",
                        "expected_signals": 10,
                    },
                )
            )
            run_id = started.run_id

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="external_callback",
                    signal_payload={"status": "initial_callback"},
                    caused_by="replay-race-callback-1",
                )
            )
            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="timeout",
                    signal_payload={"reason": "tool_timeout"},
                    caused_by="replay-race-timeout-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=2,
                lifecycle_state="waiting_external",
                recovery_reason="tool_timeout",
            )

            await asyncio.gather(
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="external_callback",
                        signal_payload={"status": "late_callback_after_timeout"},
                        caused_by="replay-race-callback-2",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="external_callback",
                        signal_payload={"status": "replayed_callback_after_timeout"},
                        caused_by="replay-race-callback-2",
                    )
                ),
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=3,
                lifecycle_state="waiting_external",
                recovery_reason="tool_timeout",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="hard_failure",
                    signal_payload={"reason": "executor_fatal"},
                    caused_by="replay-race-hard-failure-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=4,
                lifecycle_state="aborted",
                recovery_reason="executor_fatal",
            )

            await asyncio.gather(
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="timeout",
                        signal_payload={"reason": "late_timeout_after_hard_failure"},
                        caused_by="replay-race-timeout-2",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="timeout",
                        signal_payload={"reason": "replayed_timeout_after_hard_failure"},
                        caused_by="replay-race-timeout-2",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="external_callback",
                        signal_payload={"status": "late_callback_after_hard_failure"},
                        caused_by="replay-race-callback-3",
                    )
                ),
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=6,
                lifecycle_state="aborted",
                recovery_reason="executor_fatal",
            )

            await bundle.facade.signal_run(
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="cancel_requested",
                    signal_payload={"reason": "operator_cancel"},
                    caused_by="replay-race-cancel-1",
                )
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=7,
                lifecycle_state="aborted",
                recovery_reason="operator_cancel",
            )

            await asyncio.gather(
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="hard_failure",
                        signal_payload={"reason": "late_hard_failure_after_cancel"},
                        caused_by="replay-race-hard-failure-2",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="timeout",
                        signal_payload={"reason": "late_timeout_after_cancel"},
                        caused_by="replay-race-timeout-3",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="external_callback",
                        signal_payload={"status": "late_callback_after_cancel"},
                        caused_by="replay-race-callback-4",
                    )
                ),
                bundle.facade.signal_run(
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="cancel_requested",
                        signal_payload={"reason": "duplicate_operator_cancel"},
                        caused_by="replay-race-cancel-1",
                    )
                ),
            )
            await _wait_for_projection(
                bundle,
                run_id,
                min_offset=10,
                lifecycle_state="aborted",
                recovery_reason="operator_cancel",
            )

            final_projection = await bundle.facade.query_run(QueryRunRequest(run_id=run_id))
            assert final_projection.projected_offset == 10
            assert final_projection.lifecycle_state == "aborted"
            assert final_projection.recovery_mode == "abort"
            assert final_projection.recovery_reason == "operator_cancel"
            assert final_projection.waiting_external is False
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
