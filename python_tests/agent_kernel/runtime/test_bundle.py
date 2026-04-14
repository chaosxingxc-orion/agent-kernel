"""Tests for agent_kernel runtime bundle assembly and basic collaboration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_kernel.adapters.agent_core.session_adapter import AgentCoreCallbackInput
from agent_kernel.kernel.admission import SnapshotDrivenAdmissionService
from agent_kernel.kernel.cognitive.llm_gateway import EchoLLMGateway
from agent_kernel.kernel.contracts import (
    Action,
    EffectClass,
    MCPActivityInput,
    RecoveryInput,
    RunProjection,
    ToolActivityInput,
)
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    ActivityBackedExecutorService,
    InMemoryKernelRuntimeEventLog,
)
from agent_kernel.kernel.persistence.sqlite_decision_deduper import SQLiteDecisionDeduper
from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
from agent_kernel.kernel.persistence.sqlite_event_log import (
    SQLiteKernelRuntimeEventLog,
)
from agent_kernel.kernel.persistence.sqlite_recovery_outcome_store import (
    SQLiteRecoveryOutcomeStore,
)
from agent_kernel.kernel.persistence.sqlite_turn_intent_log import (
    SQLiteTurnIntentLog,
)
from agent_kernel.kernel.recovery import PlannedRecoveryGateService
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.runtime.bundle import (
    AgentKernelRuntimeBundle,
    RuntimeDecisionDedupeConfig,
    RuntimeDedupeConfig,
    RuntimeEventLogConfig,
    RuntimeProductionSafetyConfig,
    RuntimeRecoveryOutcomeConfig,
    RuntimeStrictModeConfig,
    RuntimeTurnIntentLogConfig,
)
from agent_kernel.substrate.temporal.gateway import TemporalGatewayConfig
from agent_kernel.substrate.temporal.worker import TemporalWorkerConfig


@dataclass(slots=True)
class _FakeHandle:
    """Minimal fake workflow handle for bundle tests."""

    signal_calls: list[dict[str, Any]] = field(default_factory=list)
    query_result: dict[str, Any] = field(
        default_factory=lambda: {
            "run_id": "unknown",
            "lifecycle_state": "ready",
            "projected_offset": 0,
            "waiting_external": False,
            "ready_for_dispatch": True,
            "active_child_runs": [],
        }
    )

    async def signal(self, signal_fn: str, payload: dict[str, Any]) -> None:
        del signal_fn
        self.signal_calls.append(payload)

    async def query(self, query_fn: str) -> dict[str, Any]:
        del query_fn
        return self.query_result

    async def cancel(self, reason: str) -> None:
        del reason


@dataclass(slots=True)
class _FakeTemporalClient:
    """Minimal fake Temporal client supporting bundle gateway operations."""

    started: list[dict[str, Any]] = field(default_factory=list)
    handles: dict[str, _FakeHandle] = field(default_factory=dict)

    async def start_workflow(
        self,
        workflow_fn: Any,
        run_input: Any,
        **kwargs: Any,
    ) -> None:
        del workflow_fn
        self.started.append({"run_input": run_input, "kwargs": kwargs})

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        if workflow_id not in self.handles:
            self.handles[workflow_id] = _FakeHandle()
        return self.handles[workflow_id]


@dataclass(slots=True)
class _RecordingActivityGateway:
    """Captures activity gateway invocations for executor wiring assertions."""

    tool_requests: list[ToolActivityInput] = field(default_factory=list)
    mcp_requests: list[MCPActivityInput] = field(default_factory=list)

    async def execute_tool(self, request: ToolActivityInput) -> dict[str, Any]:
        self.tool_requests.append(request)
        return {"route": "tool", "tool_name": request.tool_name}

    async def execute_mcp(self, request: MCPActivityInput) -> dict[str, Any]:
        self.mcp_requests.append(request)
        return {"route": "mcp", "server_name": request.server_name}


class _Session:
    """Minimal openjiuwen-like session object."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def session_id(self) -> str:
        return self._session_id


def test_bundle_defaults_to_in_memory_event_log_backend() -> None:
    """Bundle should keep in-memory event log backend by default."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=_FakeTemporalClient())

    assert isinstance(bundle.event_log, InMemoryKernelRuntimeEventLog)
    assert isinstance(bundle.dedupe_store, InMemoryDedupeStore)


def test_bundle_supports_sqlite_event_log_and_keeps_worker_wiring(
    tmp_path: Path,
) -> None:
    """Bundle should support sqlite event log backend without breaking wiring."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        event_log_config=RuntimeEventLogConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-event-log.sqlite3",
        ),
    )

    assert isinstance(bundle.event_log, SQLiteKernelRuntimeEventLog)
    projection = asyncio.run(bundle.projection.get("run-sqlite-1"))
    assert projection.run_id == "run-sqlite-1"
    assert isinstance(bundle.recovery, PlannedRecoveryGateService)

    dependency_bundle = bundle.create_run_actor_dependency_bundle()
    assert dependency_bundle.event_log is bundle.event_log
    assert dependency_bundle.projection is bundle.projection
    assert dependency_bundle.recovery is bundle.recovery

    worker = bundle.create_temporal_worker(
        client=object(),
        config=TemporalWorkerConfig(task_queue="bundle-worker-sqlite-q"),
    )
    dependencies = worker._dependencies  # pylint: disable=protected-access
    assert dependencies is not None
    assert dependencies.event_log is bundle.event_log
    assert dependencies.projection is bundle.projection
    assert dependencies.recovery is bundle.recovery

    bundle.event_log.close()


def test_bundle_supports_sqlite_dedupe_backend_and_worker_dependency_wiring(
    tmp_path: Path,
) -> None:
    """Bundle should build sqlite dedupe backend and wire it into workflow deps."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        dedupe_config=RuntimeDedupeConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-dedupe.sqlite3",
        ),
    )

    assert isinstance(bundle.dedupe_store, SQLiteDedupeStore)
    dependency_bundle = bundle.create_run_actor_dependency_bundle()
    assert dependency_bundle.dedupe_store is bundle.dedupe_store
    bundle.dedupe_store.close()


def test_bundle_supports_sqlite_recovery_outcome_and_turn_intent_backends(
    tmp_path: Path,
) -> None:
    """Bundle should wire sqlite recovery outcome + turn intent stores into workflow deps."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-recovery.sqlite3",
        ),
        turn_intent_log_config=RuntimeTurnIntentLogConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-turn-intent.sqlite3",
        ),
    )

    assert isinstance(bundle.recovery_outcomes, SQLiteRecoveryOutcomeStore)
    assert isinstance(bundle.turn_intent_log, SQLiteTurnIntentLog)
    dependency_bundle = bundle.create_run_actor_dependency_bundle()
    assert dependency_bundle.recovery_outcomes is bundle.recovery_outcomes
    assert dependency_bundle.turn_intent_log is bundle.turn_intent_log
    bundle.recovery_outcomes.close()
    bundle.turn_intent_log.close()


def test_bundle_builds_complete_set_and_routes_start_and_signal() -> None:
    """Bundle should wire adapter + facade + gateway collaboration end to end."""
    client = _FakeTemporalClient()
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=client,
        temporal_config=TemporalGatewayConfig(task_queue="bundle-q", workflow_id_prefix="run"),
    )
    start_request = bundle.runner_adapter.from_openjiuwen_run_call(
        runner_kind="research",
        inputs={"query": "bundle"},
        session=_Session("session-bundle-1"),
        context_ref=None,
    )

    response = asyncio.run(bundle.facade.start_run(start_request))
    assert response.run_id == "session-bundle-1:research"
    assert response.temporal_workflow_id == "run:session-bundle-1:research"
    asyncio.run(
        bundle.session_adapter.bind_run_to_session(
            session_id="session-bundle-1",
            run_id=response.run_id,
            binding_kind="primary",
        )
    )
    signal_request = bundle.session_adapter.translate_callback(
        AgentCoreCallbackInput(
            session_id="session-bundle-1",
            callback_type="tool_result",
            callback_payload={"ok": True},
            caused_by="cb-bundle-1",
        )
    )

    asyncio.run(bundle.facade.signal_run(signal_request))

    handle = client.get_workflow_handle("run:session-bundle-1:research")
    assert len(handle.signal_calls) == 1
    assert handle.signal_calls[0]["signal_type"] == "tool_result"


def test_bundle_uses_planner_driven_recovery_gate_and_produces_decision() -> None:
    """Bundle should wire PlannedRecoveryGateService as the runtime recovery gate."""
    client = _FakeTemporalClient()
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=client)

    assert isinstance(bundle.recovery, PlannedRecoveryGateService)
    decision = asyncio.run(
        bundle.recovery.decide(
            RecoveryInput(
                run_id="run-1",
                reason_code="executor_transient_error",
                lifecycle_state="recovering",
                projection=RunProjection(
                    run_id="run-1",
                    lifecycle_state="recovering",
                    projected_offset=4,
                    waiting_external=False,
                    ready_for_dispatch=False,
                ),
                failed_action_id="action-1",
            )
        )
    )
    assert decision.mode == "static_compensation"
    assert decision.reason == "recovery:executor_transient_error"
    assert decision.compensation_action_id == "action-1"


def test_bundle_honors_custom_temporal_prefix_and_task_queue_on_start() -> None:
    """Bundle should preserve custom Temporal id mapping and task routing."""
    client = _FakeTemporalClient()
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=client,
        temporal_config=TemporalGatewayConfig(
            task_queue="bundle-q",
            workflow_id_prefix="custom-prefix",
        ),
    )

    start_request = bundle.runner_adapter.from_openjiuwen_run_call(
        runner_kind="research",
        inputs={"query": "bundle"},
        session=_Session("session-bundle-2"),
        context_ref=None,
    )
    response = asyncio.run(bundle.facade.start_run(start_request))

    assert response.run_id == "session-bundle-2:research"
    assert response.temporal_workflow_id == "custom-prefix:session-bundle-2:research"
    assert len(client.started) == 1
    assert client.started[0]["kwargs"]["id"] == "custom-prefix:session-bundle-2:research"
    assert client.started[0]["kwargs"]["task_queue"] == "bundle-q"


def test_bundle_creates_run_actor_dependency_bundle_for_worker_hosting() -> None:
    """Bundle should expose dependency bundle for Temporal worker integration."""
    client = _FakeTemporalClient()
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=client)

    dependency_bundle = bundle.create_run_actor_dependency_bundle()

    assert dependency_bundle.event_log is bundle.event_log
    assert dependency_bundle.projection is bundle.projection
    assert dependency_bundle.admission is bundle.admission
    assert dependency_bundle.executor is bundle.executor
    assert dependency_bundle.recovery is bundle.recovery
    assert dependency_bundle.deduper is bundle.deduper
    assert dependency_bundle.strict_mode.enabled


def test_bundle_allows_disabling_strict_mode_for_run_actor_dependencies() -> None:
    """Bundle should wire optional strict-mode disable into workflow deps."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        strict_mode_config=RuntimeStrictModeConfig(enabled=False),
    )

    dependency_bundle = bundle.create_run_actor_dependency_bundle()

    assert not dependency_bundle.strict_mode.enabled


def test_bundle_creates_temporal_worker_with_bundle_dependencies() -> None:
    """Bundle should produce Temporal worker wired with bundle dependency bundle."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=_FakeTemporalClient())
    client = object()
    worker = bundle.create_temporal_worker(
        client=client,
        config=TemporalWorkerConfig(task_queue="bundle-worker-q"),
    )

    assert worker._client is client  # pylint: disable=protected-access
    assert worker._config.task_queue == "bundle-worker-q"  # pylint: disable=protected-access
    dependencies = worker._dependencies  # pylint: disable=protected-access
    assert dependencies is not None
    assert dependencies.event_log is bundle.event_log
    assert dependencies.projection is bundle.projection


def test_bundle_enables_activity_backed_executor_and_uses_injected_gateway() -> None:
    """Bundle should switch executor implementation when feature flag is enabled."""
    gateway = _RecordingActivityGateway()
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        enable_activity_backed_executor=True,
        activity_gateway=gateway,
    )

    assert isinstance(bundle.executor, ActivityBackedExecutorService)

    tool_result = asyncio.run(
        bundle.executor.execute(
            Action(
                action_id="action-tool-bundle-1",
                run_id="run-bundle-1",
                action_type="web_research",
                effect_class=EffectClass.READ_ONLY,
                input_json={"tool_name": "web.search", "arguments": {"q": "bundle"}},
            )
        )
    )
    mcp_result = asyncio.run(
        bundle.executor.execute(
            Action(
                action_id="action-mcp-bundle-1",
                run_id="run-bundle-1",
                action_type="web_research",
                effect_class=EffectClass.READ_ONLY,
                input_json={"mcp": {"server_name": "docs", "operation": "fetch"}},
            )
        )
    )

    assert tool_result == {"route": "tool", "tool_name": "web.search"}
    assert mcp_result == {"route": "mcp", "server_name": "docs"}
    assert len(gateway.tool_requests) == 1
    assert gateway.tool_requests[0].tool_name == "web.search"
    assert len(gateway.mcp_requests) == 1
    assert gateway.mcp_requests[0].server_name == "docs"


def test_bundle_builds_activity_gateway_from_handler_maps_and_routes() -> None:
    """Bundle should build strict activity gateway from explicit handler maps."""
    tool_requests: list[ToolActivityInput] = []
    mcp_requests: list[MCPActivityInput] = []

    async def tool_handler(request: ToolActivityInput) -> dict[str, Any]:
        tool_requests.append(request)
        return {"route": "tool", "tool_name": request.tool_name}

    async def mcp_handler(request: MCPActivityInput) -> dict[str, Any]:
        mcp_requests.append(request)
        return {"route": "mcp", "server_name": request.server_name}

    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        enable_activity_backed_executor=True,
        tool_handlers={"web.search": tool_handler},
        mcp_handlers={("docs", "fetch"): mcp_handler},
    )

    tool_result = asyncio.run(
        bundle.executor.execute(
            Action(
                action_id="action-tool-bundle-2",
                run_id="run-bundle-2",
                action_type="web_research",
                effect_class=EffectClass.READ_ONLY,
                input_json={"tool_name": "web.search", "arguments": {"q": "bundle"}},
            )
        )
    )
    mcp_result = asyncio.run(
        bundle.executor.execute(
            Action(
                action_id="action-mcp-bundle-2",
                run_id="run-bundle-2",
                action_type="web_research",
                effect_class=EffectClass.READ_ONLY,
                input_json={"mcp": {"server_name": "docs", "operation": "fetch"}},
            )
        )
    )

    assert isinstance(bundle.executor, ActivityBackedExecutorService)
    assert tool_result == {"route": "tool", "tool_name": "web.search"}
    assert mcp_result == {"route": "mcp", "server_name": "docs"}
    assert len(tool_requests) == 1
    assert tool_requests[0].tool_name == "web.search"
    assert len(mcp_requests) == 1
    assert mcp_requests[0].server_name == "docs"


def test_bundle_prod_safety_rejects_in_memory_defaults_in_prod() -> None:
    """Prod safety mode should reject in-memory defaults."""
    with pytest.raises(ValueError, match="production safety check failed"):
        AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient(),
            production_safety_config=RuntimeProductionSafetyConfig(
                enabled=True,
                environment="prod",
            ),
        )


def test_bundle_prod_safety_accepts_sqlite_backends_in_prod(tmp_path: Path) -> None:
    """Prod safety mode should allow bundle build with persisted backends."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        event_log_config=RuntimeEventLogConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-prod-event-log.sqlite3",
        ),
        dedupe_config=RuntimeDedupeConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-prod-dedupe.sqlite3",
        ),
        recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-prod-recovery.sqlite3",
        ),
        turn_intent_log_config=RuntimeTurnIntentLogConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "bundle-prod-turn-intent.sqlite3",
        ),
        enable_activity_backed_executor=True,
        activity_gateway=_RecordingActivityGateway(),
        production_safety_config=RuntimeProductionSafetyConfig(
            enabled=True,
            environment="prod",
        ),
    )

    assert isinstance(bundle.event_log, SQLiteKernelRuntimeEventLog)
    assert isinstance(bundle.dedupe_store, SQLiteDedupeStore)
    assert isinstance(bundle.recovery_outcomes, SQLiteRecoveryOutcomeStore)
    assert isinstance(bundle.turn_intent_log, SQLiteTurnIntentLog)
    bundle.event_log.close()
    bundle.dedupe_store.close()
    bundle.recovery_outcomes.close()
    bundle.turn_intent_log.close()


def test_bundle_prod_safety_rejects_echo_llm_gateway_in_prod(tmp_path: Path) -> None:
    """Prod safety mode should block EchoLLMGateway."""
    with pytest.raises(ValueError, match="EchoLLMGateway"):
        AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient(),
            event_log_config=RuntimeEventLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "bundle-prod-event-log2.sqlite3",
            ),
            dedupe_config=RuntimeDedupeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "bundle-prod-dedupe2.sqlite3",
            ),
            recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "bundle-prod-recovery2.sqlite3",
            ),
            turn_intent_log_config=RuntimeTurnIntentLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "bundle-prod-turn-intent2.sqlite3",
            ),
            llm_gateway=EchoLLMGateway(),
            production_safety_config=RuntimeProductionSafetyConfig(
                enabled=True,
                environment="prod",
            ),
        )


def test_bundle_default_deduper_is_sqlite_decision_deduper() -> None:
    """Default decision deduper should be SQLiteDecisionDeduper (in-memory SQLite)."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=_FakeTemporalClient())
    assert isinstance(bundle.deduper, SQLiteDecisionDeduper)


def test_bundle_decision_deduper_config_in_memory_backend() -> None:
    """Explicit in_memory backend should produce InMemoryDecisionDeduper."""
    from agent_kernel.kernel.minimal_runtime import InMemoryDecisionDeduper

    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        decision_deduper_config=RuntimeDecisionDedupeConfig(backend="in_memory"),
    )
    assert isinstance(bundle.deduper, InMemoryDecisionDeduper)


def test_bundle_decision_deduper_config_sqlite_backend(tmp_path: Path) -> None:
    """Explicit sqlite backend should produce SQLiteDecisionDeduper backed by file."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(
        temporal_client=_FakeTemporalClient(),
        decision_deduper_config=RuntimeDecisionDedupeConfig(
            backend="sqlite",
            sqlite_database_path=tmp_path / "decision-deduper.sqlite3",
        ),
    )
    assert isinstance(bundle.deduper, SQLiteDecisionDeduper)


def test_bundle_default_admission_is_snapshot_driven() -> None:
    """Default admission service should be SnapshotDrivenAdmissionService."""
    bundle = AgentKernelRuntimeBundle.build_minimal_complete(temporal_client=_FakeTemporalClient())
    assert isinstance(bundle.admission, SnapshotDrivenAdmissionService)


def test_bundle_prod_safety_rejects_in_memory_decision_deduper(
    tmp_path: Path,
) -> None:
    """Prod safety mode should block decision_deduper backend=in_memory."""
    with pytest.raises(ValueError, match="decision_deduper"):
        AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient(),
            event_log_config=RuntimeEventLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "safety-event-log.sqlite3",
            ),
            dedupe_config=RuntimeDedupeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "safety-dedupe.sqlite3",
            ),
            decision_deduper_config=RuntimeDecisionDedupeConfig(backend="in_memory"),
            recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "safety-recovery.sqlite3",
            ),
            turn_intent_log_config=RuntimeTurnIntentLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "safety-turn-intent.sqlite3",
            ),
            production_safety_config=RuntimeProductionSafetyConfig(
                enabled=True,
                environment="prod",
            ),
        )


class TestBundleTaskRegistryWiring:
    """Tests for task_registry field wiring in AgentKernelRuntimeBundle."""

    def test_bundle_has_task_registry_field(self) -> None:
        """Bundle should expose a non-None task_registry after build."""
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient()
        )

        assert bundle.task_registry is not None

    def test_bundle_task_registry_wired_to_facade(self) -> None:
        """Facade's _task_registry should be the same object as bundle.task_registry."""
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient()
        )

        assert bundle.facade._task_registry is bundle.task_registry  # pylint: disable=protected-access

    def test_bundle_task_registry_is_task_registry_instance(self) -> None:
        """bundle.task_registry should be a TaskRegistry instance."""
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient()
        )

        assert isinstance(bundle.task_registry, TaskRegistry)

    def test_bundle_task_registry_has_event_log(self) -> None:
        """TaskRegistry should be constructed with a non-None event appender."""
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient()
        )

        assert bundle.task_registry._event_appender is not None  # pylint: disable=protected-access


class TestProductionSafetyExecutorCheck:
    """Tests for the no-op executor production safety guard."""

    def test_production_safety_rejects_no_op_executor(self, tmp_path: Path) -> None:
        """Prod safety should reject bundle built with no-op AsyncExecutorService."""
        with pytest.raises(ValueError, match="no-op AsyncExecutorService"):
            AgentKernelRuntimeBundle.build_minimal_complete(
                temporal_client=_FakeTemporalClient(),
                event_log_config=RuntimeEventLogConfig(
                    backend="sqlite",
                    sqlite_database_path=tmp_path / "exec-check-event-log.sqlite3",
                ),
                dedupe_config=RuntimeDedupeConfig(
                    backend="sqlite",
                    sqlite_database_path=tmp_path / "exec-check-dedupe.sqlite3",
                ),
                recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
                    backend="sqlite",
                    sqlite_database_path=tmp_path / "exec-check-recovery.sqlite3",
                ),
                turn_intent_log_config=RuntimeTurnIntentLogConfig(
                    backend="sqlite",
                    sqlite_database_path=tmp_path / "exec-check-turn-intent.sqlite3",
                ),
                enable_activity_backed_executor=False,
                production_safety_config=RuntimeProductionSafetyConfig(
                    enabled=True,
                    environment="prod",
                ),
            )

    def test_production_safety_allows_activity_backed_executor(self, tmp_path: Path) -> None:
        """Prod safety should pass when enable_activity_backed_executor=True with gateway."""
        gateway = _RecordingActivityGateway()
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=_FakeTemporalClient(),
            event_log_config=RuntimeEventLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "exec-allow-event-log.sqlite3",
            ),
            dedupe_config=RuntimeDedupeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "exec-allow-dedupe.sqlite3",
            ),
            recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "exec-allow-recovery.sqlite3",
            ),
            turn_intent_log_config=RuntimeTurnIntentLogConfig(
                backend="sqlite",
                sqlite_database_path=tmp_path / "exec-allow-turn-intent.sqlite3",
            ),
            enable_activity_backed_executor=True,
            activity_gateway=gateway,
            production_safety_config=RuntimeProductionSafetyConfig(
                enabled=True,
                environment="prod",
            ),
        )
        assert isinstance(bundle.executor, ActivityBackedExecutorService)
        bundle.event_log.close()
        bundle.dedupe_store.close()
        bundle.recovery_outcomes.close()
        bundle.turn_intent_log.close()

    def test_async_executor_service_no_handler_allowed_in_tests(self) -> None:
        """AsyncExecutorService() without a handler is allowed outside prod mode."""
        from agent_kernel.kernel.minimal_runtime import AsyncExecutorService

        # Construction must not raise in test/dev mode.
        svc = AsyncExecutorService()
        assert svc._handler is None
