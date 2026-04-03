"""Temporal run-actor workflow for the agent_kernel kernel.

Design constraints:
  - The workflow is the *only* lifecycle authority for a run.
  - Event truth lives in ``KernelRuntimeEventLog``.
  - Projection truth lives in ``DecisionProjectionService``.
  - Recovery truth lives in ``RecoveryGateService``.
  - The workflow never embeds business logic.

Why a Temporal workflow?
  - Durable execution guarantees that a run never silently
    disappears.
  - Signal-based wake-up maps naturally to external callbacks.
  - Query-based projection keeps read-path load off the event
    log.
"""

from __future__ import annotations

import contextlib
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuilder
from agent_kernel.kernel.capability_snapshot_resolver import (
    ActionPayloadCapabilitySnapshotInputResolver,
)
from agent_kernel.kernel.contracts import (
    ActionCommit,
    DecisionDeduper,
    DecisionProjectionService,
    DispatchAdmissionService,
    ExecutorService,
    KernelRuntimeEventLog,
    ObservabilityHook,
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
    RecoveryOutcome,
    RecoveryOutcomeStore,
    RunProjection,
    RuntimeEvent,
    TurnIntentLog,
    TurnIntentRecord,
)
from agent_kernel.kernel.dedupe_store import DedupeStorePort, InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput, TurnResult

try:
    from temporalio import workflow as temporal_workflow
    from temporalio.exceptions import TemporalError
except ImportError:  # pragma: no cover - optional dependency in CI
    TemporalError = RuntimeError
    temporal_workflow = None


@dataclass(frozen=True, slots=True)
class RunInput:
    """Represents the workflow start payload.

    Attributes:
        trace_context: W3C ``traceparent`` value forwarded from the caller so
            Temporal activities join the originating distributed trace.
    """

    run_id: str
    session_id: str | None = None
    parent_run_id: str | None = None
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    runtime_context_ref: str | None = None
    trace_context: str | None = None


@dataclass(frozen=True, slots=True)
class ActorSignal:
    """Represents one signal delivered to the workflow."""

    signal_type: str
    signal_payload: dict[str, Any] | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class RunActorStrictModeConfig:
    """Configures strict capability snapshot requirements for workflow turns.

    Attributes:
        enabled: Enables strict snapshot guardrails that require both
            ``capability_snapshot_input`` and
            ``declarative_bundle_digest`` in action payloads.
    """

    enabled: bool = True


@dataclass(slots=True)
class RunActorDependencyBundle:
    """Carries workflow service dependencies used by RunActorWorkflow."""

    event_log: KernelRuntimeEventLog
    projection: DecisionProjectionService
    admission: DispatchAdmissionService
    executor: ExecutorService
    recovery: RecoveryGateService
    deduper: DecisionDeduper
    dedupe_store: DedupeStorePort | None = None
    recovery_outcomes: RecoveryOutcomeStore | None = None
    turn_intent_log: TurnIntentLog | None = None
    strict_mode: RunActorStrictModeConfig = field(default_factory=RunActorStrictModeConfig)
    workflow_id_prefix: str = "run"
    observability_hook: ObservabilityHook | None = None
    context_port: Any | None = None  # ContextPort Protocol
    llm_gateway: Any | None = None  # LLMGateway Protocol
    output_parser: Any | None = None  # OutputParser Protocol
    reflection_policy: Any | None = None  # ReflectionPolicy
    reflection_builder: Any | None = None  # ReflectionContextBuilder


_RUN_ACTOR_CONFIG: ContextVar[RunActorDependencyBundle | None] = ContextVar(
    "run_actor_dependencies",
    default=None,
)
_RUN_ACTOR_CONFIG_FALLBACK: dict[str, RunActorDependencyBundle | None] = {"dependencies": None}
_RUN_ACTOR_CONFIG_FALLBACK_LOCK = threading.Lock()


def configure_run_actor_dependencies(
    dependencies: RunActorDependencyBundle | None,
) -> RunActorDependencyBundle | None:
    """Configures default dependencies for workflow construction.

    Temporal workers may execute workflow code in runtime contexts that do not
    preserve ``ContextVar`` bindings from test or bootstrap call sites.
    Maintain both context-local and process-level fallback bindings so workflow
    tests and local workers receive the same configured services deterministically.

    Returns:
        The ``dependencies`` argument, allowing callers to use the return value
        as an identity token for a scoped ``clear_run_actor_dependencies`` call.

        Args:
            dependencies: (description)
    """
    _RUN_ACTOR_CONFIG.set(dependencies)
    with _RUN_ACTOR_CONFIG_FALLBACK_LOCK:
        _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] = dependencies
    return dependencies


def clear_run_actor_dependencies(
    token: RunActorDependencyBundle | None,
) -> None:
    """Clears process-level dependencies only when they match ``token``.

    Safe to call concurrently: if another ``KernelRuntime`` has already
    registered its own bundle, this call is a no-op and the new runtime's
    state is not disturbed.

    Args:
        token: The bundle reference returned by ``configure_run_actor_dependencies``
            at start time.  Only clears if the current global matches this token.
    """
    with _RUN_ACTOR_CONFIG_FALLBACK_LOCK:
        if _RUN_ACTOR_CONFIG_FALLBACK.get("dependencies") is token:
            _RUN_ACTOR_CONFIG.set(None)
            _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] = None


class RunActorWorkflow:
    """Owns authoritative lifecycle progression for one run.

    Args:
        event_log: Authoritative kernel event log.
        projection: Decision projection service used for catch-up and readiness.
        admission: Dispatch-time admission service.
        executor: Executor service for action side effects.
        recovery: Recovery gate for all execution failures.
        deduper: Fingerprint deduper used to suppress duplicate decisions.
    """

    def __init__(
        self,
        event_log: KernelRuntimeEventLog | None = None,
        projection: DecisionProjectionService | None = None,
        admission: DispatchAdmissionService | None = None,
        executor: ExecutorService | None = None,
        recovery: RecoveryGateService | None = None,
        deduper: DecisionDeduper | None = None,
        dedupe_store: DedupeStorePort | None = None,
        recovery_outcomes: RecoveryOutcomeStore | None = None,
        turn_intent_log: TurnIntentLog | None = None,
        strict_mode: RunActorStrictModeConfig | None = None,
        turn_engine: TurnEngine | None = None,
    ) -> None:
        dependencies = _resolve_run_actor_dependencies(
            event_log=event_log,
            projection=projection,
            admission=admission,
            executor=executor,
            recovery=recovery,
            recovery_outcomes=recovery_outcomes,
            turn_intent_log=turn_intent_log,
            deduper=deduper,
            dedupe_store=dedupe_store,
            strict_mode=strict_mode,
        )
        self._event_log = dependencies.event_log
        self._projection = dependencies.projection
        self._admission = dependencies.admission
        self._executor = dependencies.executor
        self._recovery = dependencies.recovery
        self._recovery_outcomes = dependencies.recovery_outcomes
        self._turn_intent_log = dependencies.turn_intent_log
        self._deduper = dependencies.deduper
        self._dedupe_store = dependencies.dedupe_store or InMemoryDedupeStore()
        self._strict_mode = dependencies.strict_mode
        # Build optional reasoning loop when all cognitive services are present.
        _reasoning_loop = None
        if (
            dependencies.context_port is not None
            and dependencies.llm_gateway is not None
            and dependencies.output_parser is not None
        ):
            from agent_kernel.kernel.reasoning_loop import ReasoningLoop

            _reasoning_loop = ReasoningLoop(
                context_port=dependencies.context_port,
                llm_gateway=dependencies.llm_gateway,
                output_parser=dependencies.output_parser,
                observability_hook=dependencies.observability_hook,
            )

        self._turn_engine = turn_engine or TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=self._admission,
            dedupe_store=self._dedupe_store,
            executor=_TurnEngineExecutorAdapter(self._executor),
            snapshot_input_resolver=ActionPayloadCapabilitySnapshotInputResolver(
                require_declared_snapshot_input=self._strict_mode.enabled,
                require_declarative_bundle_digest=self._strict_mode.enabled,
            ),
            require_declared_snapshot_inputs=self._strict_mode.enabled,
            reasoning_loop=_reasoning_loop,
            observability_hook=dependencies.observability_hook,
        )
        self._run_id: str | None = None
        self._session_id: str | None = None
        # Temporal Python SDK query handlers should be synchronous. Keep an
        # in-memory projection snapshot that signal/run paths refresh, so query
        # can stay sync and still return up-to-date kernel state.
        self._last_projection: RunProjection | None = None
        self._pending_signals: list[ActorSignal] = []
        self._signal_sequence: int = 0
        self._seen_signal_tokens: set[str] = set()

    async def run(self, input_value: RunInput) -> dict[str, Any]:
        """Starts the workflow loop for one run.

        Args:
            input_value: Workflow start payload.

        Returns:
            A workflow completion payload.
        """
        self._run_id = input_value.run_id
        self._session_id = input_value.session_id
        self._last_projection = await self._projection.get(input_value.run_id)
        if self._pending_signals:
            pending_signals = list(self._pending_signals)
            self._pending_signals.clear()
            for pending_signal in pending_signals:
                await self._handle_signal(pending_signal)

        # agent_kernel keeps lifecycle authority in projection state. The workflow-level
        # result only mirrors terminal intent and does not invent new state.
        final_state = (
            "aborted" if self._last_projection.lifecycle_state == "aborted" else "completed"
        )
        result = {
            "run_id": input_value.run_id,
            "final_state": final_state,
        }
        await self._notify_parent_child_completed(input_value.parent_run_id)
        return result

    async def _notify_parent_child_completed(self, parent_run_id: str | None) -> None:
        """Best-effort notifies parent workflow that this child run completed.

        This callback closes the minimal parent/child lifecycle loop:
          - parent receives ``child_completed`` signal,
          - parent projection removes the child from ``active_child_runs``.

        The notification is intentionally soft-fail. Child completion should not
        crash when Temporal is unavailable, when running outside workflow event
        loop, or when parent workflow is already closed.

        Args:
            parent_run_id: Parent run identifier from workflow start input.
        """
        if not parent_run_id or not self._run_id:
            return
        if temporal_workflow is None or not _is_temporal_workflow_context():
            return

        parent_workflow_id = _workflow_id_for_parent_run(parent_run_id)
        child_signal_payload = {
            "signal_type": "child_completed",
            "signal_payload": {"child_run_id": self._run_id},
            "caused_by": f"child:{self._run_id}",
        }

        try:
            parent_handle = temporal_workflow.get_external_workflow_handle(parent_workflow_id)
            await parent_handle.signal("signal", child_signal_payload)
        except TemporalError, RuntimeError:
            # Parent notification is best-effort; do not fail child completion.
            return

    async def signal(self, input_value: ActorSignal) -> None:
        """Handles one signal routed into the workflow.

        Args:
            input_value: Signal payload for the run actor.
        """
        if not self._run_id:
            # Temporal can deliver signals before ``run()`` initializes local
            # identity fields. Buffer and replay once ``run()`` is ready.
            self._pending_signals.append(input_value)
            return

        await self._handle_signal(input_value)

    async def _handle_signal(self, input_value: ActorSignal) -> None:
        """Applies one signal to authoritative commit+decision chain."""
        if not self._run_id:
            raise RuntimeError("RunActorWorkflow.run must be called first.")

        if input_value.caused_by:
            signal_token = f"{input_value.signal_type}:{input_value.caused_by}"
            if signal_token in self._seen_signal_tokens:
                return
            self._seen_signal_tokens.add(signal_token)

        self._signal_sequence += 1
        projection = await self._projection.get(self._run_id)
        next_offset = projection.projected_offset + 1
        normalized_signal_payload = _normalize_signal_payload(
            signal_type=input_value.signal_type,
            signal_payload=input_value.signal_payload,
        )

        # Signal commit ids are deterministic for callback-originated signals.
        # This lets DecisionDeduper suppress duplicated decision rounds when the
        # same callback is replayed or retried.
        dedupe_token = input_value.caused_by or f"seq-{self._signal_sequence}"
        signal_commit = ActionCommit(
            run_id=self._run_id,
            commit_id=f"signal:{input_value.signal_type}:{dedupe_token}",
            created_at=_utc_now_iso(),
            caused_by=input_value.caused_by,
            action=None,
            events=[
                RuntimeEvent(
                    run_id=self._run_id,
                    event_id=f"evt-signal-{self._signal_sequence}",
                    commit_offset=next_offset,
                    event_type=_signal_event_type(input_value.signal_type),
                    event_class="fact",
                    event_authority="authoritative_fact",
                    ordering_key=self._run_id,
                    wake_policy="wake_actor",
                    payload_json=normalized_signal_payload,
                    created_at=_utc_now_iso(),
                )
            ],
        )

        await self._event_log.append_action_commit(signal_commit)
        await self.process_action_commit(signal_commit)
        # process_action_commit() refreshes _last_projection via catch_up().
        # Avoid awaiting here so query state remains driven by sync-safe cache.

    def query(self) -> RunProjection:
        """Returns the current authoritative projection for the run.

        Temporal deprecates asynchronous workflow query handlers. Query methods
        should be synchronous and side-effect free so they can run safely during
        workflow replay without scheduling awaits.

        Compatibility strategy:
          1. Keep ``query()`` synchronous for Temporal's recommended contract.
          2. Refresh ``self._last_projection`` from async workflow paths
             (``run()``, ``signal()``, ``process_action_commit()``).
          3. Return the cached snapshot here so gateway/query call sites do not
             need any behavior changes.

        Returns:
            The current authoritative run projection.

        Raises:
            RuntimeError: If ``run()`` has not established workflow identity.
        """
        if not self._run_id:
            raise RuntimeError("RunActorWorkflow.run must be called first.")
        if self._last_projection is None:
            raise RuntimeError("RunActorWorkflow projection is not initialized.")
        return self._last_projection

    async def process_action_commit(self, commit: ActionCommit) -> None:
        """Processes one action commit through the workflow decision chain.

        Args:
            commit: Action-level commit that should trigger one authoritative
                decision round.


        Raises:
            Exception: (description)
        """
        _assert_no_derived_diagnostic_authority_input(commit)
        projection = await self._projection.catch_up(
            commit.run_id,
            commit.events[-1].commit_offset,
        )
        self._last_projection = projection
        ready = await self._projection.readiness(
            commit.run_id,
            commit.events[-1].commit_offset,
        )
        if not ready:
            return

        fingerprint = f"{commit.run_id}:{commit.commit_id}:{commit.events[-1].commit_offset}"
        if await self._deduper.seen(fingerprint):
            return

        await self._deduper.mark(fingerprint)

        if commit.action is None:
            return

        try:
            turn_result = await self._turn_engine.run_turn(
                TurnInput(
                    run_id=commit.run_id,
                    through_offset=commit.events[-1].commit_offset,
                    based_on_offset=commit.events[-1].commit_offset,
                    trigger_type="signal",
                ),
                commit.action,
            )
            _assert_single_dispatch_attempt_in_turn(turn_result)
            next_offset = await self._append_turn_outcome_commit(
                run_id=commit.run_id,
                offset=commit.events[-1].commit_offset + 1,
                turn_result=turn_result,
                caused_by=commit.action.action_id,
            )
            if turn_result.outcome_kind == "dispatched":
                # Notify circuit breaker of successful dispatch so it can
                # reset the failure counter for this effect class.
                _on_success = getattr(self._recovery, "on_action_success", None)
                if callable(_on_success):
                    with contextlib.suppress(Exception):
                        _result = _on_success(commit.action.effect_class)
                        # on_action_success is sync; if a mock returns a
                        # coroutine, discard it cleanly to avoid ResourceWarning.
                        if hasattr(_result, "close"):
                            _result.close()
            if turn_result.outcome_kind == "recovery_pending":
                offset_before_recovery_decide = await _latest_run_offset(
                    self._event_log,
                    commit.run_id,
                )
                recovery_decision = await self._recovery.decide(
                    RecoveryInput(
                        run_id=commit.run_id,
                        failed_action_id=commit.action.action_id,
                        failed_effect_class=commit.action.effect_class,
                        reason_code=(
                            turn_result.recovery_input.failure_code
                            if turn_result.recovery_input is not None
                            else "effect_unknown"
                        ),
                        lifecycle_state=projection.lifecycle_state,
                        projection=projection,
                    )
                )
                offset_after_recovery_decide = await _latest_run_offset(
                    self._event_log,
                    commit.run_id,
                )
                _assert_recovery_gate_is_read_only(
                    before_offset=offset_before_recovery_decide,
                    after_offset=offset_after_recovery_decide,
                )
                await self._append_recovery_event(
                    run_id=commit.run_id,
                    offset=next_offset,
                    recovery_decision=recovery_decision,
                    caused_by=commit.action.action_id,
                )
        # Recovery gate must observe all executor failures regardless of
        # concrete exception type, so this catch intentionally stays broad.
        except Exception as err:  # pylint: disable=broad-exception-caught
            offset_before_recovery_decide = await _latest_run_offset(
                self._event_log,
                commit.run_id,
            )
            recovery_decision = await self._recovery.decide(
                RecoveryInput(
                    run_id=commit.run_id,
                    failed_action_id=commit.action.action_id,
                    failed_effect_class=commit.action.effect_class,
                    reason_code=type(err).__name__.lower(),
                    lifecycle_state=projection.lifecycle_state,
                    projection=projection,
                )
            )
            offset_after_recovery_decide = await _latest_run_offset(
                self._event_log,
                commit.run_id,
            )
            _assert_recovery_gate_is_read_only(
                before_offset=offset_before_recovery_decide,
                after_offset=offset_after_recovery_decide,
            )
            await self._append_recovery_event(
                run_id=commit.run_id,
                offset=commit.events[-1].commit_offset + 1,
                recovery_decision=recovery_decision,
                caused_by=commit.action.action_id,
            )
            raise

    async def _append_turn_outcome_commit(
        self,
        run_id: str,
        offset: int,
        turn_result: TurnResult,
        caused_by: str | None,
    ) -> int:
        """Appends one authoritative commit that records TurnEngine outcome.

        ``TurnResult`` is an in-memory workflow artifact unless persisted to the
        runtime event log. This helper ensures canonical turn outcomes are
        durable and replay-observable by writing one normalized ``run.*`` fact.

        Args:
            run_id: Run identifier that owns the turn.
            offset: Runtime event offset to assign in this commit envelope.
            turn_result: Deterministic result returned by ``TurnEngine``.
            caused_by: Action identifier that produced the turn, when present.

        Returns:
            The next offset value after this commit.
        """
        event = RuntimeEvent(
            run_id=run_id,
            event_id=f"evt-turn-{turn_result.outcome_kind}-{offset}",
            commit_offset=offset,
            event_type=_turn_outcome_event_type(turn_result.outcome_kind),
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key=run_id,
            wake_policy="wake_actor",
            idempotency_key=turn_result.decision_fingerprint,
            payload_json=_build_turn_outcome_payload(turn_result),
            created_at=_utc_now_iso(),
        )
        if self._turn_intent_log is not None and turn_result.intent_commit_ref is not None:
            await self._turn_intent_log.write_intent(
                TurnIntentRecord(
                    run_id=run_id,
                    intent_commit_ref=turn_result.intent_commit_ref,
                    decision_ref=turn_result.decision_ref,
                    decision_fingerprint=turn_result.decision_fingerprint,
                    dispatch_dedupe_key=turn_result.dispatch_dedupe_key,
                    host_kind=turn_result.host_kind,
                    outcome_kind=turn_result.outcome_kind,
                    written_at=_utc_now_iso(),
                )
            )
        await self._event_log.append_action_commit(
            ActionCommit(
                run_id=run_id,
                commit_id=f"turn:{turn_result.outcome_kind}:{offset}",
                created_at=_utc_now_iso(),
                caused_by=caused_by,
                action=None,
                events=[event],
            )
        )
        return offset + 1

    async def _append_recovery_event(
        self,
        run_id: str,
        offset: int,
        recovery_decision: RecoveryDecision,
        caused_by: str | None,
    ) -> int:
        """Appends one recovery decision event to authoritative runtime log.

        Emits two events in the same commit:
          1. ``recovery.plan_selected`` (derived_diagnostic) — the planner
             decision is always auditable even if execution fails later.
          2. The authoritative recovery fact (authoritative_fact) — the
             durable lifecycle state change consumed by projection replay.
        """
        event_type = _recovery_event_type(recovery_decision.mode)
        _assert_recovery_event_type_allowed(event_type)
        await self._event_log.append_action_commit(
            ActionCommit(
                run_id=run_id,
                commit_id=f"recovery:{event_type}:{offset}",
                created_at=_utc_now_iso(),
                caused_by=caused_by,
                action=None,
                events=[
                    RuntimeEvent(
                        run_id=run_id,
                        event_id=f"evt-recovery-plan-{offset}",
                        commit_offset=offset,
                        event_type="recovery.plan_selected",
                        event_class="derived",
                        event_authority="derived_diagnostic",
                        ordering_key=run_id,
                        wake_policy="projection_only",
                        payload_json={
                            "planned_mode": recovery_decision.mode,
                            "reason": recovery_decision.reason,
                            "compensation_action_id": (recovery_decision.compensation_action_id),
                            "escalation_channel_ref": (recovery_decision.escalation_channel_ref),
                        },
                        created_at=_utc_now_iso(),
                    ),
                    RuntimeEvent(
                        run_id=run_id,
                        event_id=f"evt-recovery-{offset}",
                        commit_offset=offset + 1,
                        event_type=event_type,
                        event_class="fact",
                        event_authority="authoritative_fact",
                        ordering_key=run_id,
                        wake_policy="wake_actor",
                        payload_json={
                            "mode": recovery_decision.mode,
                            "reason": recovery_decision.reason,
                        },
                        created_at=_utc_now_iso(),
                    ),
                ],
            )
        )
        if self._recovery_outcomes is not None:
            await self._recovery_outcomes.write_outcome(
                RecoveryOutcome(
                    run_id=run_id,
                    action_id=caused_by,
                    recovery_mode=recovery_decision.mode,
                    outcome_state=_recovery_outcome_state(recovery_decision.mode),
                    operator_escalation_ref=(recovery_decision.escalation_channel_ref),
                    emitted_event_ids=[
                        f"evt-recovery-plan-{offset}",
                        f"evt-recovery-{offset}",
                    ],
                    written_at=_utc_now_iso(),
                )
            )
        return offset + 2


class _TurnEngineExecutorAdapter:
    """Adapts workflow ExecutorService to TurnEngine executor port."""

    def __init__(self, executor: ExecutorService) -> None:
        """Initializes adapter with executor service.

        Args:
            executor: Executor service to delegate action execution.
        """
        self._executor = executor

    async def execute(
        self,
        action: Any,
        _snapshot: Any,
        _envelope: Any,
        execution_context: Any | None = None,
    ) -> dict[str, Any]:
        """Executes action through existing executor and normalizes ack payload.

        Args:
            action: Admitted action to execute.
            _snapshot: Capability snapshot, accepted but unused.
            _envelope: Idempotency envelope, accepted but unused.

        Returns:
            Normalized acknowledgement dictionary.
        """
        del execution_context
        result = await self._executor.execute(action, grant_ref=None)
        if isinstance(result, dict):
            return result
        return {"acknowledged": bool(result)}


def _utc_now_iso() -> str:
    """Returns an RFC3339 UTC timestamp for kernel event envelopes.

    Returns:
        UTC timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _is_temporal_workflow_context() -> bool:
    """Returns whether code is currently running inside Temporal workflow loop.

    Returns:
        ``True`` when running inside a Temporal workflow context.
    """
    if temporal_workflow is None:
        return False

    in_workflow = getattr(temporal_workflow, "in_workflow", None)
    if callable(in_workflow):
        try:
            return bool(in_workflow())
        except RuntimeError:
            return False

    # Compatibility fallback for SDK variants without ``in_workflow`` helper.
    try:
        temporal_workflow.info()
    except RuntimeError:
        return False
    return True


def _workflow_id_for_parent_run(parent_run_id: str) -> str:
    """Builds parent workflow id from run id using gateway default prefix.

    Args:
        parent_run_id: Parent run identifier.

    Returns:
        Temporal workflow id string for the parent run.
    """
    if parent_run_id.startswith("run:"):
        return parent_run_id
    return f"run:{parent_run_id}"


def _signal_event_type(signal_type: str) -> str:
    """Maps signal types to authoritative runtime event taxonomy.

    Signal names are transport-level inputs. Lifecycle authority still belongs
    to runtime event projection, so this mapping upgrades selected signals to
    stable ``run.*`` facts that the projection layer can reason about.

    Args:
        signal_type: Raw signal type string from transport layer.

    Returns:
        Mapped authoritative runtime event type string.
    """
    mapped_event_type = _SIGNAL_EVENT_TYPE_MAP.get(signal_type)
    if mapped_event_type is not None:
        return mapped_event_type
    return f"signal.{signal_type}"


def _normalize_signal_payload(
    signal_type: str,
    signal_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalizes signal payloads for deterministic replay and projection semantics.

    Args:
        signal_type: Signal type discriminator for normalization rules.
        signal_payload: Raw signal payload dictionary, may be ``None``.

    Returns:
        Normalized signal payload, or ``None`` when empty after normalization.
    """
    if signal_payload is None:
        normalized_payload: dict[str, Any] = {}
    else:
        normalized_payload = dict(signal_payload)

    if signal_type == "cancel_requested":
        normalized_payload.setdefault("reason", "cancel_requested")
    elif signal_type == "hard_failure":
        normalized_payload.setdefault("mode", "abort")
        normalized_payload.setdefault("reason", "hard_failure")
    elif signal_type == "timeout":
        normalized_payload.setdefault("mode", "human_escalation")
        normalized_payload.setdefault("reason", "timeout")

    if not normalized_payload:
        return None
    return normalized_payload


_SIGNAL_EVENT_TYPE_MAP: dict[str, str] = {
    "resume_from_snapshot": "run.resume_requested",
    # Cancellation is modeled as an explicit lifecycle request fact. Projection
    # remains the only authority that transitions the run into terminal state.
    "cancel_requested": "run.cancel_requested",
    "hard_failure": "run.recovery_aborted",
    "timeout": "run.waiting_external",
    "recovery_succeeded": "run.recovery_succeeded",
    "recovery_aborted": "run.recovery_aborted",
}


def _build_turn_outcome_payload(turn_result: TurnResult) -> dict[str, Any]:
    """Builds replay-safe payload for persisted turn outcome runtime events.

    Args:
        turn_result: Deterministic turn result to serialize.

    Returns:
        Normalized payload dictionary for event persistence.
    """
    payload: dict[str, Any] = {
        "state": turn_result.state,
        "outcome_kind": turn_result.outcome_kind,
        "decision_ref": turn_result.decision_ref,
        "decision_fingerprint": turn_result.decision_fingerprint,
    }
    if turn_result.intent_commit_ref is not None:
        payload["intent_commit_ref"] = turn_result.intent_commit_ref
    if turn_result.dispatch_dedupe_key is not None:
        payload["dispatch_dedupe_key"] = turn_result.dispatch_dedupe_key
    if turn_result.host_kind is not None:
        payload["host_kind"] = turn_result.host_kind

    remote_policy_payload = _build_remote_policy_decision_payload(turn_result)
    if remote_policy_payload is not None:
        payload["remote_policy_decision"] = remote_policy_payload

    emitted_states = _extract_turn_emitted_states(turn_result.emitted_events)
    if emitted_states:
        payload["emitted_states"] = emitted_states

    if turn_result.action_commit is not None:
        payload["action_commit"] = dict(turn_result.action_commit)

    if turn_result.recovery_input is not None:
        payload["recovery_input"] = _build_recovery_input_payload(turn_result)

    return payload


def _build_remote_policy_decision_payload(
    turn_result: TurnResult,
) -> dict[str, Any] | None:
    """Builds optional remote policy decision payload from ``TurnResult``.

    Args:
        turn_result: Turn result with optional remote policy decision.

    Returns:
        Remote policy decision dictionary, or ``None`` when absent.
    """
    remote_policy_decision = turn_result.remote_policy_decision
    if remote_policy_decision is None:
        return None
    return {
        "effective_idempotency_level": remote_policy_decision.effective_idempotency_level,
        "default_retry_policy": remote_policy_decision.default_retry_policy,
        "auto_retry_enabled": remote_policy_decision.auto_retry_enabled,
        "can_claim_guaranteed": remote_policy_decision.can_claim_guaranteed,
        "reason": remote_policy_decision.reason,
    }


def _extract_turn_emitted_states(
    emitted_events: list[Any],
) -> list[str]:
    """Extracts ordered ``state`` markers from TurnEngine emitted events.

    Args:
        emitted_events: List of TurnStateEvent objects.

    Returns:
        Ordered list of non-empty state string values.
    """
    emitted_states: list[str] = []
    for emitted_event in emitted_events:
        state_value = emitted_event.state
        if isinstance(state_value, str) and state_value != "":
            emitted_states.append(state_value)
    return emitted_states


def _build_recovery_input_payload(turn_result: TurnResult) -> dict[str, Any]:
    """Builds normalized recovery evidence payload from ``TurnResult``.

    Args:
        turn_result: Turn result with optional recovery input.

    Returns:
        Recovery evidence dictionary, or empty dict when absent.
    """
    recovery_input = turn_result.recovery_input
    if recovery_input is None:
        return {}
    return {
        "failure_code": recovery_input.failure_code,
        "failed_stage": recovery_input.failed_stage,
        "failed_component": recovery_input.failed_component,
        "failure_class": recovery_input.failure_class,
        "evidence_priority_source": recovery_input.evidence_priority_source,
        "evidence_priority_ref": recovery_input.evidence_priority_ref,
    }


def _turn_outcome_event_type(outcome_kind: str) -> str:
    """Maps TurnEngine outcome kind to canonical authoritative ``run.*`` event.

    Args:
        outcome_kind: Turn outcome kind discriminator.

    Returns:
        Canonical runtime event type string.

    Raises:
        ValueError: If ``outcome_kind`` is not recognized.
    """
    if outcome_kind == "dispatched":
        return "run.dispatching"
    if outcome_kind == "recovery_pending":
        return "run.recovering"
    if outcome_kind in ("blocked", "noop"):
        # ``blocked`` and ``noop`` preserve dispatch eligibility in canonical
        # path; ``run.ready`` keeps projection semantics stable while still
        # persisting that a full turn round completed.
        return "run.ready"
    raise ValueError(f"Unsupported TurnEngine outcome_kind: {outcome_kind}")


def _recovery_event_type(mode: str) -> str:
    """Maps recovery decision mode to runtime event type.

    Args:
        mode: Recovery decision mode string.

    Returns:
        Canonical recovery runtime event type string.
    """
    if mode == "abort":
        return "run.recovery_aborted"
    if mode == "human_escalation":
        return "run.waiting_external"
    return "run.recovering"


def _recovery_outcome_state(
    mode: str,
) -> str:
    """Maps recovery mode to minimal persisted recovery outcome state.

    Args:
        mode: Recovery decision mode string.

    Returns:
        Persisted recovery outcome state string.
    """
    if mode == "abort":
        return "aborted"
    if mode == "human_escalation":
        return "escalated"
    return "executed"


async def _latest_run_offset(event_log: KernelRuntimeEventLog, run_id: str) -> int:
    """Returns latest commit offset observed for run from authoritative event log."""
    events = await event_log.load(run_id, after_offset=0)
    if not isinstance(events, list) or not events:
        return 0
    return max(event.commit_offset for event in events)


def _assert_no_derived_diagnostic_authority_input(commit: ActionCommit) -> None:
    """Guards authority input path from derived diagnostic events."""
    for event in commit.events:
        if event.event_authority == "derived_diagnostic":
            raise ValueError("derived_diagnostic events must not enter authority input path.")


def _assert_single_dispatch_attempt_in_turn(turn_result: TurnResult) -> None:
    """Ensures one turn contains at most one authoritative dispatch state."""
    dispatch_count = sum(1 for event in turn_result.emitted_events if event.state == "dispatched")
    if dispatch_count > 1:
        raise RuntimeError("Single turn must not contain multiple authoritative dispatch attempts.")


def _assert_recovery_gate_is_read_only(before_offset: int, after_offset: int) -> None:
    """Ensures recovery gate does not mutate lifecycle/event truth directly."""
    if after_offset != before_offset:
        raise RuntimeError(
            "Recovery gate must be read-only and cannot append runtime events directly."
        )


def _assert_recovery_event_type_allowed(event_type: str) -> None:
    """Ensures recovery append path emits only recovery-class event types."""
    if event_type not in ("run.recovering", "run.waiting_external", "run.recovery_aborted"):
        raise RuntimeError(f"Recovery event type is not allowed for lifecycle safety: {event_type}")


def _resolve_run_actor_dependencies(
    event_log: KernelRuntimeEventLog | None,
    projection: DecisionProjectionService | None,
    admission: DispatchAdmissionService | None,
    executor: ExecutorService | None,
    recovery: RecoveryGateService | None,
    recovery_outcomes: RecoveryOutcomeStore | None,
    turn_intent_log: TurnIntentLog | None,
    deduper: DecisionDeduper | None,
    dedupe_store: DedupeStorePort | None,
    strict_mode: RunActorStrictModeConfig | None,
) -> RunActorDependencyBundle:
    """Resolves workflow dependencies from explicit args, config, or defaults.

    Args:
        event_log: Optional explicit event log service.
        projection: Optional explicit projection service.
        admission: Optional explicit admission service.
        executor: Optional explicit executor service.
        recovery: Optional explicit recovery gate service.
        recovery_outcomes: Optional explicit recovery outcome store.
        turn_intent_log: Optional explicit turn intent store.
        deduper: Optional explicit decision deduper.
        dedupe_store: Optional explicit dedupe store.
        strict_mode: Optional strict mode configuration.

    Returns:
        Fully resolved dependency bundle for workflow construction.
    """
    if all(
        dependency is not None
        for dependency in (
            event_log,
            projection,
            admission,
            executor,
            recovery,
            deduper,
        )
    ):
        return RunActorDependencyBundle(
            event_log=event_log,
            projection=projection,
            admission=admission,
            executor=executor,
            recovery=recovery,
            dedupe_store=dedupe_store,
            recovery_outcomes=recovery_outcomes,
            turn_intent_log=turn_intent_log,
            deduper=deduper,
            strict_mode=strict_mode or RunActorStrictModeConfig(),
        )

    configured_dependencies = _RUN_ACTOR_CONFIG.get()
    if configured_dependencies is not None:
        return RunActorDependencyBundle(
            event_log=configured_dependencies.event_log,
            projection=configured_dependencies.projection,
            admission=configured_dependencies.admission,
            executor=configured_dependencies.executor,
            recovery=configured_dependencies.recovery,
            dedupe_store=(
                dedupe_store if dedupe_store is not None else configured_dependencies.dedupe_store
            ),
            recovery_outcomes=(
                recovery_outcomes
                if recovery_outcomes is not None
                else configured_dependencies.recovery_outcomes
            ),
            turn_intent_log=(
                turn_intent_log
                if turn_intent_log is not None
                else configured_dependencies.turn_intent_log
            ),
            deduper=configured_dependencies.deduper,
            strict_mode=(
                strict_mode if strict_mode is not None else configured_dependencies.strict_mode
            ),
        )
    with _RUN_ACTOR_CONFIG_FALLBACK_LOCK:
        fallback_dependencies = _RUN_ACTOR_CONFIG_FALLBACK["dependencies"]
    if fallback_dependencies is not None:
        return RunActorDependencyBundle(
            event_log=fallback_dependencies.event_log,
            projection=fallback_dependencies.projection,
            admission=fallback_dependencies.admission,
            executor=fallback_dependencies.executor,
            recovery=fallback_dependencies.recovery,
            dedupe_store=(
                dedupe_store if dedupe_store is not None else fallback_dependencies.dedupe_store
            ),
            recovery_outcomes=(
                recovery_outcomes
                if recovery_outcomes is not None
                else fallback_dependencies.recovery_outcomes
            ),
            turn_intent_log=(
                turn_intent_log
                if turn_intent_log is not None
                else fallback_dependencies.turn_intent_log
            ),
            deduper=fallback_dependencies.deduper,
            strict_mode=(
                strict_mode if strict_mode is not None else fallback_dependencies.strict_mode
            ),
        )

    default_event_log = InMemoryKernelRuntimeEventLog()
    return RunActorDependencyBundle(
        event_log=default_event_log,
        projection=InMemoryDecisionProjectionService(default_event_log),
        admission=StaticDispatchAdmissionService(),
        executor=AsyncExecutorService(),
        recovery=StaticRecoveryGateService(),
        dedupe_store=dedupe_store,
        recovery_outcomes=None,
        turn_intent_log=turn_intent_log,
        deduper=InMemoryDecisionDeduper(),
        strict_mode=strict_mode or RunActorStrictModeConfig(),
    )
