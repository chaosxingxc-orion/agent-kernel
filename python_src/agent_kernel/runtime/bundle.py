"""Runtime bundle that wires the minimal complete agent_kernel component set."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_kernel.adapters.agent_core.checkpoint_adapter import (
    AgentCoreCheckpointAdapter,
)
from agent_kernel.adapters.agent_core.context_adapter import (
    AgentCoreContextAdapter,
)
from agent_kernel.adapters.agent_core.runner_adapter import (
    AgentCoreRunnerAdapter,
)
from agent_kernel.adapters.agent_core.session_adapter import (
    AgentCoreSessionAdapter,
)
from agent_kernel.adapters.agent_core.tool_mcp_adapter import (
    AgentCoreToolMCPAdapter,
)
from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    AdmissionResult,
    DecisionDeduper,
    DecisionProjectionService,
    DispatchAdmissionService,
    ExecutorService,
    KernelRuntimeEventLog,
    RecoveryGateService,
    RecoveryOutcomeStore,
    TemporalActivityGateway,
    TemporalWorkflowGateway,
    TurnIntentLog,
)
from agent_kernel.kernel.dedupe_store import DedupeStorePort, InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    ActivityBackedExecutorService,
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    InMemoryRecoveryOutcomeStore,
    StaticDispatchAdmissionService,
)
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
from agent_kernel.kernel.recovery import (
    PlannedRecoveryGateService,
    RecoveryPlanner,
)
from agent_kernel.substrate.temporal.activity_gateway import (
    MCPActivityCallable,
    MCPHandlerKey,
    TemporalActivityBindings,
    TemporalSDKActivityGateway,
    ToolActivityCallable,
)
from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
)
from agent_kernel.substrate.temporal.worker import (
    TemporalKernelWorker,
    TemporalWorkerConfig,
)

EventLogBackend = Literal["in_memory", "sqlite"]
DedupeBackend = Literal["in_memory", "sqlite"]
RecoveryOutcomeBackend = Literal["in_memory", "sqlite"]
TurnIntentBackend = Literal["none", "sqlite"]


@dataclass(frozen=True, slots=True)
class RuntimeEventLogConfig:
    """Declares which event log backend the runtime bundle should use.

    Attributes:
        backend: Storage backend kind used to create the runtime
            event log.
        sqlite_database_path: SQLite database file path when
            ``backend`` is ``"sqlite"``. Use ``":memory:"`` for
            process-local in-memory SQLite.
    """

    backend: EventLogBackend = "in_memory"
    sqlite_database_path: str | Path = ":memory:"


@dataclass(frozen=True, slots=True)
class RuntimeDedupeConfig:
    """Declares dedupe store backend used by runtime bundle.

    Attributes:
        backend: Dedupe backend kind.
        sqlite_database_path: SQLite path when ``backend`` is ``"sqlite"``.
    """

    backend: DedupeBackend = "in_memory"
    sqlite_database_path: str | Path = ":memory:"


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryOutcomeConfig:
    """Declares recovery outcome store backend used by runtime bundle."""

    backend: RecoveryOutcomeBackend = "in_memory"
    sqlite_database_path: str | Path = ":memory:"


@dataclass(frozen=True, slots=True)
class RuntimeTurnIntentLogConfig:
    """Declares turn intent log backend used by runtime bundle."""

    backend: TurnIntentBackend = "none"
    sqlite_database_path: str | Path = ":memory:"


@dataclass(frozen=True, slots=True)
class RuntimeStrictModeConfig:
    """Declares strict snapshot requirements used by workflow turn execution.

    Attributes:
        enabled: When ``True`` (default), workflow turn execution requires
            declared ``capability_snapshot_input`` and
            ``declarative_bundle_digest`` payloads.
    """

    enabled: bool = True


@dataclass(slots=True)
class AgentKernelRuntimeBundle:
    """Holds the wired minimal-complete runtime component set.

    Attributes:
        event_log: Kernel runtime event log service.
        projection: Decision projection service for run state.
        admission: Dispatch admission service for action evaluation.
        executor: Action executor service for dispatch.
        recovery: Recovery gate service for failure handling.
        deduper: Decision deduper for fingerprint deduplication.
        dedupe_store: Dedupe store port for idempotency management.
        strict_mode_config: Strict mode configuration for turn execution.
        gateway: Temporal workflow gateway for substrate communication.
        facade: Kernel facade for external API boundary.
        runner_adapter: Agent-core runner adapter for platform integration.
        session_adapter: Agent-core session adapter for session binding.
        context_adapter: Agent-core context adapter for context management.
        checkpoint_adapter: Agent-core checkpoint adapter for checkpoint views.
        tool_mcp_adapter: Agent-core tool/MCP adapter for binding resolution.
    """

    event_log: KernelRuntimeEventLog
    projection: DecisionProjectionService
    admission: DispatchAdmissionService
    executor: ExecutorService
    recovery: RecoveryGateService
    recovery_outcomes: RecoveryOutcomeStore | None
    deduper: DecisionDeduper
    dedupe_store: DedupeStorePort
    turn_intent_log: TurnIntentLog | None
    strict_mode_config: RuntimeStrictModeConfig
    gateway: TemporalWorkflowGateway
    facade: KernelFacade
    runner_adapter: AgentCoreRunnerAdapter
    session_adapter: AgentCoreSessionAdapter
    context_adapter: AgentCoreContextAdapter
    checkpoint_adapter: AgentCoreCheckpointAdapter
    tool_mcp_adapter: AgentCoreToolMCPAdapter

    @classmethod
    def build_minimal_complete(
        cls,
        temporal_client: Any,
        temporal_config: TemporalGatewayConfig | None = None,
        event_log_config: RuntimeEventLogConfig | None = None,
        dedupe_config: RuntimeDedupeConfig | None = None,
        recovery_outcome_config: RuntimeRecoveryOutcomeConfig | None = None,
        turn_intent_log_config: RuntimeTurnIntentLogConfig | None = None,
        strict_mode_config: RuntimeStrictModeConfig | None = None,
        enable_activity_backed_executor: bool = False,
        activity_gateway: TemporalActivityGateway | None = None,
        tool_handlers: (
            Mapping[str, ToolActivityCallable] | None
        ) = None,
        mcp_handlers: (
            Mapping[MCPHandlerKey, MCPActivityCallable] | None
        ) = None,
    ) -> AgentKernelRuntimeBundle:
        """Builds one minimal-complete runtime bundle.

        Args:
            temporal_client: Temporal client used by substrate
                gateway and worker.
            temporal_config: Optional Temporal gateway behavior
                overrides.
            event_log_config: Optional event log backend
                configuration.
            dedupe_config: Optional dedupe backend configuration.
            recovery_outcome_config: Optional recovery outcome store
                backend configuration.
            turn_intent_log_config: Optional turn intent log backend
                configuration.
            strict_mode_config: Optional strict-mode toggle used by
                workflow turn snapshot wiring. Defaults to strict
                mode enabled.
            enable_activity_backed_executor: Enables executor
                implementation that delegates tool/MCP execution
                to ``TemporalActivityGateway``. Defaults to
                ``False`` to preserve previous in-memory executor
                behavior.
            activity_gateway: Gateway instance required when
                ``enable_activity_backed_executor`` is ``True``.
            tool_handlers: Optional tool handlers keyed by
                ``tool_name`` used to build a strict
                ``TemporalSDKActivityGateway`` when an explicit
                ``activity_gateway`` is not provided.
            mcp_handlers: Optional MCP handlers keyed by
                ``(server_name, capability)`` used to build a
                strict ``TemporalSDKActivityGateway`` when an
                explicit ``activity_gateway`` is not provided.

        Returns:
            A fully wired runtime bundle using the selected event
            log backend.
        """
        kernel_core = cls._build_kernel_core(
            event_log_config=event_log_config,
            dedupe_config=dedupe_config,
            recovery_outcome_config=recovery_outcome_config,
            turn_intent_log_config=turn_intent_log_config,
            enable_activity_backed_executor=(
                enable_activity_backed_executor
            ),
            activity_gateway=activity_gateway,
            tool_handlers=tool_handlers,
            mcp_handlers=mcp_handlers,
        )
        boundary = cls._build_boundary_components(
            temporal_client=temporal_client,
            temporal_config=temporal_config,
        )
        return cls(
            event_log=kernel_core["event_log"],
            projection=kernel_core["projection"],
            admission=kernel_core["admission"],
            executor=kernel_core["executor"],
            recovery=kernel_core["recovery"],
            recovery_outcomes=kernel_core["recovery_outcomes"],
            deduper=kernel_core["deduper"],
            dedupe_store=kernel_core["dedupe_store"],
            turn_intent_log=kernel_core["turn_intent_log"],
            strict_mode_config=(
                strict_mode_config or RuntimeStrictModeConfig()
            ),
            gateway=boundary["gateway"],
            facade=boundary["facade"],
            runner_adapter=boundary["runner_adapter"],
            session_adapter=boundary["session_adapter"],
            context_adapter=boundary["context_adapter"],
            checkpoint_adapter=boundary["checkpoint_adapter"],
            tool_mcp_adapter=boundary["tool_mcp_adapter"],
        )

    @staticmethod
    def _build_kernel_core(
        event_log_config: RuntimeEventLogConfig | None = None,
        dedupe_config: RuntimeDedupeConfig | None = None,
        recovery_outcome_config: RuntimeRecoveryOutcomeConfig | None = None,
        turn_intent_log_config: RuntimeTurnIntentLogConfig | None = None,
        enable_activity_backed_executor: bool = False,
        activity_gateway: TemporalActivityGateway | None = None,
        tool_handlers: (
            Mapping[str, ToolActivityCallable] | None
        ) = None,
        mcp_handlers: (
            Mapping[MCPHandlerKey, MCPActivityCallable] | None
        ) = None,
    ) -> dict[str, Any]:
        """Builds minimal kernel core services.

        Args:
            event_log_config: Optional event log backend
                configuration.
            dedupe_config: Optional dedupe backend configuration.
            recovery_outcome_config: Optional recovery outcome store
                backend configuration.
            turn_intent_log_config: Optional turn intent log backend
                configuration.
            enable_activity_backed_executor: Whether to wire
                activity-backed executor implementation.
            activity_gateway: Optional activity gateway dependency
                used when activity-backed execution is enabled.
            tool_handlers: Optional tool handlers for strict
                activity gateway construction.
            mcp_handlers: Optional MCP handlers for strict
                activity gateway construction.

        Returns:
            Dictionary of kernel core service instances for
            bundle assembly.
        """
        event_log = AgentKernelRuntimeBundle._build_event_log(
            event_log_config or RuntimeEventLogConfig(),
        )
        dedupe_store = AgentKernelRuntimeBundle._build_dedupe_store(
            dedupe_config or RuntimeDedupeConfig(),
        )
        recovery_outcomes = AgentKernelRuntimeBundle._build_recovery_outcomes(
            recovery_outcome_config or RuntimeRecoveryOutcomeConfig(),
        )
        turn_intent_log = AgentKernelRuntimeBundle._build_turn_intent_log(
            turn_intent_log_config or RuntimeTurnIntentLogConfig(),
        )
        recovery_planner = RecoveryPlanner()
        return {
            "event_log": event_log,
            "projection": InMemoryDecisionProjectionService(
                event_log,
            ),
            "admission": StaticDispatchAdmissionService(),
            "executor": AgentKernelRuntimeBundle._build_executor(
                enable_activity_backed_executor=(
                    enable_activity_backed_executor
                ),
                activity_gateway=activity_gateway,
                tool_handlers=tool_handlers,
                mcp_handlers=mcp_handlers,
            ),
            "recovery": PlannedRecoveryGateService(
                planner=recovery_planner,
            ),
            "recovery_outcomes": recovery_outcomes,
            "deduper": InMemoryDecisionDeduper(),
            "dedupe_store": dedupe_store,
            "turn_intent_log": turn_intent_log,
        }

    @staticmethod
    def _build_executor(
        enable_activity_backed_executor: bool,
        activity_gateway: TemporalActivityGateway | None,
        tool_handlers: (
            Mapping[str, ToolActivityCallable] | None
        ) = None,
        mcp_handlers: (
            Mapping[MCPHandlerKey, MCPActivityCallable] | None
        ) = None,
    ) -> ExecutorService:
        """Builds executor service from feature-toggle and deps.

        Args:
            enable_activity_backed_executor: Whether to use
                activity-backed executor.
            activity_gateway: Optional activity gateway instance.
            tool_handlers: Optional tool handler mappings.
            mcp_handlers: Optional MCP handler mappings.

        Returns:
            Executor service instance.

        Raises:
            ValueError: If activity-backed executor is enabled
                without gateway dependency or handler
                registrations.
        """
        if not enable_activity_backed_executor:
            return AsyncExecutorService()
        resolved_gateway = (
            AgentKernelRuntimeBundle._resolve_activity_gateway(
                activity_gateway=activity_gateway,
                tool_handlers=tool_handlers,
                mcp_handlers=mcp_handlers,
            )
        )
        if resolved_gateway is None:
            raise ValueError(
                "activity_gateway or explicit tool/mcp handlers"
                " are required when"
                " enable_activity_backed_executor is True."
            )
        return ActivityBackedExecutorService(resolved_gateway)

    @staticmethod
    def _resolve_activity_gateway(
        activity_gateway: TemporalActivityGateway | None,
        tool_handlers: (
            Mapping[str, ToolActivityCallable] | None
        ),
        mcp_handlers: (
            Mapping[MCPHandlerKey, MCPActivityCallable] | None
        ),
    ) -> TemporalActivityGateway | None:
        """Resolves activity gateway from dependency or handlers.

        Args:
            activity_gateway: Explicit activity gateway instance.
            tool_handlers: Optional tool handler mappings.
            mcp_handlers: Optional MCP handler mappings.

        Returns:
            Resolved activity gateway or None.

        Raises:
            ValueError: If both explicit gateway and handler maps
                are provided.
        """
        has_tool_handlers = bool(tool_handlers)
        has_mcp_handlers = bool(mcp_handlers)
        if activity_gateway is not None:
            if has_tool_handlers or has_mcp_handlers:
                raise ValueError(
                    "Pass either activity_gateway or"
                    " tool/mcp handlers, not both."
                )
            return activity_gateway
        if not has_tool_handlers and not has_mcp_handlers:
            return None
        return (
            AgentKernelRuntimeBundle
            ._build_activity_gateway_from_handlers(
                tool_handlers=tool_handlers,
                mcp_handlers=mcp_handlers,
            )
        )

    @staticmethod
    def _build_activity_gateway_from_handlers(
        tool_handlers: (
            Mapping[str, ToolActivityCallable] | None
        ),
        mcp_handlers: (
            Mapping[MCPHandlerKey, MCPActivityCallable] | None
        ),
    ) -> TemporalActivityGateway:
        """Builds strict Temporal activity gateway from handlers.

        Args:
            tool_handlers: Tool handler mappings.
            mcp_handlers: MCP handler mappings.

        Returns:
            TemporalSDKActivityGateway instance.
        """
        return TemporalSDKActivityGateway(
            TemporalActivityBindings(
                admission_activity=(
                    lambda _request: AdmissionResult(
                        admitted=True,
                        reason_code="ok",
                    )
                ),
                tool_activity=lambda _request: None,
                mcp_activity=lambda _request: None,
                verification_activity=(
                    lambda _request: {}
                ),
                reconciliation_activity=(
                    lambda _request: {}
                ),
            ),
            tool_handlers=tool_handlers,
            mcp_handlers=mcp_handlers,
        )

    @staticmethod
    def _build_event_log(
        event_log_config: RuntimeEventLogConfig,
    ) -> KernelRuntimeEventLog:
        """Builds event log backend from configuration.

        Args:
            event_log_config: Event log backend selection and
                backend options.

        Returns:
            Concrete event log instance that satisfies
            ``KernelRuntimeEventLog``.

        Raises:
            ValueError: If backend value is not supported.
        """
        if event_log_config.backend == "in_memory":
            return InMemoryKernelRuntimeEventLog()
        if event_log_config.backend == "sqlite":
            return SQLiteKernelRuntimeEventLog(
                event_log_config.sqlite_database_path,
            )
        raise ValueError(
            f"Unsupported event log backend:"
            f" {event_log_config.backend}"
        )

    @staticmethod
    def _build_dedupe_store(
        dedupe_config: RuntimeDedupeConfig,
    ) -> DedupeStorePort:
        """Builds dedupe backend from configuration.

        Args:
            dedupe_config: Dedupe backend selection and backend options.

        Returns:
            Concrete dedupe store implementation.

        Raises:
            ValueError: If backend value is not supported.
        """
        if dedupe_config.backend == "in_memory":
            return InMemoryDedupeStore()
        if dedupe_config.backend == "sqlite":
            return SQLiteDedupeStore(dedupe_config.sqlite_database_path)
        raise ValueError(
            f"Unsupported dedupe backend: {dedupe_config.backend}"
        )

    @staticmethod
    def _build_recovery_outcomes(
        recovery_outcome_config: RuntimeRecoveryOutcomeConfig,
    ) -> RecoveryOutcomeStore:
        """Builds recovery outcome store backend from configuration."""
        if recovery_outcome_config.backend == "in_memory":
            return InMemoryRecoveryOutcomeStore()
        if recovery_outcome_config.backend == "sqlite":
            return SQLiteRecoveryOutcomeStore(recovery_outcome_config.sqlite_database_path)
        raise ValueError(
            "Unsupported recovery_outcome backend: "
            f"{recovery_outcome_config.backend}"
        )

    @staticmethod
    def _build_turn_intent_log(
        turn_intent_log_config: RuntimeTurnIntentLogConfig,
    ) -> TurnIntentLog | None:
        """Builds turn intent log backend from configuration."""
        if turn_intent_log_config.backend == "none":
            return None
        if turn_intent_log_config.backend == "sqlite":
            return SQLiteTurnIntentLog(turn_intent_log_config.sqlite_database_path)
        raise ValueError(
            "Unsupported turn_intent_log backend: "
            f"{turn_intent_log_config.backend}"
        )

    @staticmethod
    def _build_boundary_components(
        temporal_client: Any,
        temporal_config: TemporalGatewayConfig | None,
    ) -> dict[str, Any]:
        """Builds gateway, facade, and agent-core boundary adapters.

        Args:
            temporal_client: Temporal client for substrate.
            temporal_config: Optional Temporal gateway config.

        Returns:
            Dictionary of boundary component instances.
        """
        gateway = TemporalSDKWorkflowGateway(
            temporal_client, temporal_config,
        )
        context_adapter = AgentCoreContextAdapter()
        checkpoint_adapter = AgentCoreCheckpointAdapter()
        session_adapter = AgentCoreSessionAdapter()
        runner_adapter = AgentCoreRunnerAdapter()
        tool_mcp_adapter = AgentCoreToolMCPAdapter()
        facade = KernelFacade(
            workflow_gateway=gateway,
            context_adapter=context_adapter,
            checkpoint_adapter=checkpoint_adapter,
        )
        return {
            "gateway": gateway,
            "facade": facade,
            "runner_adapter": runner_adapter,
            "session_adapter": session_adapter,
            "context_adapter": context_adapter,
            "checkpoint_adapter": checkpoint_adapter,
            "tool_mcp_adapter": tool_mcp_adapter,
        }

    def create_run_actor_dependency_bundle(
        self,
    ) -> RunActorDependencyBundle:
        """Creates workflow dependency bundle for Temporal worker.

        Returns:
            RunActorDependencyBundle wired with bundle services.
        """
        return RunActorDependencyBundle(
            event_log=self.event_log,
            projection=self.projection,
            admission=self.admission,
            executor=self.executor,
            recovery=self.recovery,
            recovery_outcomes=self.recovery_outcomes,
            turn_intent_log=self.turn_intent_log,
            deduper=self.deduper,
            dedupe_store=self.dedupe_store,
            strict_mode=RunActorStrictModeConfig(
                enabled=self.strict_mode_config.enabled
            ),
        )

    def create_temporal_worker(
        self,
        client: Any,
        config: TemporalWorkerConfig | None = None,
    ) -> TemporalKernelWorker:
        """Creates worker wired with this bundle's dependencies.

        Args:
            client: Temporal client instance.
            config: Optional worker configuration.

        Returns:
            TemporalKernelWorker wired with bundle dependencies.
        """
        return TemporalKernelWorker(
            client=client,
            config=config,
            dependencies=self.create_run_actor_dependency_bundle(),
        )
