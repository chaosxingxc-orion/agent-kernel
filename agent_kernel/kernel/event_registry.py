"""Event type registry for agent-kernel v6.4.

Provides a central catalog of all kernel-emitted RuntimeEvent event_type
values, preventing semantic pollution from ad-hoc string additions.

Usage::

    from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY

    descriptor = KERNEL_EVENT_REGISTRY.get("run.started")
    assert descriptor is not None
    print(descriptor.description)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_registry_logger = logging.getLogger(__name__)

_CURRENT_EVENT_SCHEMA_VERSION = "1"
"""Current schema version for RuntimeEvent.  Bump when the event DTO fields
change in a backward-incompatible way."""


@dataclass(frozen=True, slots=True)
class EventTypeDescriptor:
    """Describes a single registered RuntimeEvent event_type value.

    Attributes:
        event_type: Canonical event type string (e.g. ``"run.started"``).
        description: Human-readable summary of when/why this event fires.
        authority: Which of the six core authorities emits this event.
        affects_replay: Whether this event participates in recovery replay.
            Set ``False`` for ``derived_diagnostic`` events.
        recovery_path_allowed: Whether this event type may be emitted by the
            Recovery append path (``_append_recovery_event``).  Only the three
            recovery-class lifecycle facts are allowed there; all others are
            rejected by ``_assert_recovery_event_type_allowed``.
        schema_version: The RuntimeEvent schema version this descriptor was
            written for.  Validated by ``validate_event_schema_version`` to
            detect stale deserialized events from older deployments.
    """

    event_type: str
    description: str
    authority: str
    affects_replay: bool = True
    recovery_path_allowed: bool = False
    schema_version: str = _CURRENT_EVENT_SCHEMA_VERSION


@dataclass(slots=True)
class EventTypeRegistry:
    """Central registry of known RuntimeEvent event_type values.

    Raises ``ValueError`` on duplicate registration to prevent accidental
    shadowing.  Teams extending the kernel should call ``register()`` at
    module import time, not at request time.
    """

    _entries: dict[str, EventTypeDescriptor] = field(default_factory=dict)

    def register(self, descriptor: EventTypeDescriptor) -> None:
        """Register a new event type descriptor.

        Args:
            descriptor: The descriptor to register.

        Raises:
            ValueError: When ``descriptor.event_type`` is already registered.
        """
        if descriptor.event_type in self._entries:
            raise ValueError(
                f"Event type '{descriptor.event_type}' is already registered. "
                "Use a unique event_type string or update the existing entry."
            )
        self._entries[descriptor.event_type] = descriptor

    def get(self, event_type: str) -> EventTypeDescriptor | None:
        """Return descriptor for the given event_type, or ``None``.

        Args:
            event_type: The event type string to look up.

        Returns:
            Matching descriptor, or ``None`` when not registered.
        """
        return self._entries.get(event_type)

    def all(self) -> list[EventTypeDescriptor]:
        """Return all registered descriptors sorted by event_type.

        Returns:
            Alphabetically sorted list of all registered descriptors.
        """
        return sorted(self._entries.values(), key=lambda d: d.event_type)

    def known_types(self) -> frozenset[str]:
        """Return frozenset of all registered event_type strings.

        Returns:
            Immutable set of registered event type strings.
        """
        return frozenset(self._entries.keys())


# ---------------------------------------------------------------------------
# Kernel-built-in event type registry
# ---------------------------------------------------------------------------

KERNEL_EVENT_REGISTRY: EventTypeRegistry = EventTypeRegistry()

_KERNEL_EVENTS: list[EventTypeDescriptor] = [
    # --- Run lifecycle ---
    EventTypeDescriptor(
        event_type="run.created",
        description=(
            "Initial lifecycle fact emitted when the run is "
            "first accepted by RunActor and the projection is "
            "seeded into 'created' state."
        ),
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.started",
        description="RunActor has accepted the run and entered activelifecycle.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.completed",
        description="RunActor has finished all turns and exited normally.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.aborted",
        description="RunActor was externally cancelled or fatally errored.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.child_completed",
        description="A child run signalled its completion back to this parentrun.",
        authority="RunActor",
        affects_replay=True,
    ),
    # --- Turn / TurnEngine FSM ---
    EventTypeDescriptor(
        event_type="turn.intent_committed",
        description="TurnEngine committed the action intent; FSM advanced tointent_committed.",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.snapshot_built",
        description="CapabilitySnapshot was deterministically built and hashed.",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.admission_checked",
        description="Admission gate evaluated the snapshot and approveddispatch.",
        authority="Admission",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.dispatch_blocked",
        description="Admission gate blocked dispatch; action will not beexecuted.",
        authority="Admission",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.dispatched",
        description="DedupeStore recorded dispatch reservation; Executorcalled.",
        authority="Executor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.dispatch_acknowledged",
        description="Executor returned a confirmed external acknowledgement.",
        authority="Executor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.effect_unknown",
        description="Executor call completed but outcome cannot be determined.",
        authority="Executor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.effect_recorded",
        description="Executor effect was confirmed and written toRuntimeEventLog.",
        authority="Executor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.completed_noop",
        description="Turn completed with no external effect (e.g. admissionblocked, noop action).",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.recovery_pending",
        description="Recovery is pending for this turn; outcome not yetdetermined.",
        authority="Recovery",
        affects_replay=True,
    ),
    # --- Parallel branch events (diagnostic, not replayed) ---
    EventTypeDescriptor(
        event_type="turn.branch_dispatched",
        description="A parallel branch action was dispatched to the executor.",
        authority="Executor",
        affects_replay=False,
    ),
    EventTypeDescriptor(
        event_type="turn.branch_acknowledged",
        description="A parallel branch action was acknowledged by the executor.",
        authority="Executor",
        affects_replay=False,
    ),
    EventTypeDescriptor(
        event_type="turn.branch_failed",
        description="A parallel branch action failed or timed out.",
        authority="Executor",
        affects_replay=False,
    ),
    EventTypeDescriptor(
        event_type="turn.parallel_joined",
        description="All branches in a parallel group completed and the join"
        "strategy was evaluated.",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.branch_rollback_intent",
        description=(
            "A parallel group join failed; rollback intent was signalled for one "
            "or more succeeded branches.  Diagnostic only — compensating "
            "transactions must be driven by the caller."
        ),
        authority="Executor",
        affects_replay=False,
    ),
    # --- Recovery ---
    EventTypeDescriptor(
        event_type="recovery.plan_selected",
        description=(
            "Recovery planner selected a plan (action + reason) for a failed turn. "
            "Emitted before execution so the decision is always auditable even if "
            "execution itself fails."
        ),
        authority="Recovery",
        affects_replay=False,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="recovery.outcome_recorded",
        description="Recovery authority recorded a final outcome for a failedturn.",
        authority="Recovery",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="recovery.human_escalation",
        description="Recovery escalated to human review; run is paused.",
        authority="Recovery",
        affects_replay=True,
    ),
    # --- Signals ---
    EventTypeDescriptor(
        event_type="signal.received",
        description="A signal was received by the RunActor from an externalcaller.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="signal.child_completed",
        description="A child-completion signal was received and processed.",
        authority="RunActor",
        affects_replay=True,
    ),
    # --- Run lifecycle (substrate-emitted state facts) ---
    EventTypeDescriptor(
        event_type="run.dispatching",
        description="Run entered dispatching state; an action was admitted anddispatched.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.recovering",
        description="Run entered recovery state; a failed action is beinghandled.",
        authority="RunActor",
        affects_replay=True,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="run.ready",
        description="Run returned to ready state after a blocked/noop turn.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.waiting_result",
        description=(
            "Run is waiting for the result of a dispatched action; "
            "used by heartbeat monitor to detect stuck tool/MCP calls."
        ),
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.recovery_aborted",
        description="Recovery decided to abort the run; terminal state follows.",
        authority="Recovery",
        affects_replay=True,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="run.waiting_external",
        description="Run is paused waiting for an external event or humanreview.",
        authority="RunActor",
        affects_replay=True,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="run.waiting_human_input",
        description=(
            "Run is paused waiting for an explicit human interaction — either an "
            "approval gate, a clarification request, or a human-in-the-loop review. "
            "Distinct from waiting_external to allow finer-grained heartbeat policy "
            "and recovery routing for human-paced tasks."
        ),
        authority="RunActor",
        affects_replay=True,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="run.cancel_requested",
        description="An external cancellation request was received.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.resume_requested",
        description="A resume-from-snapshot signal was received.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.plan_submitted",
        description=(
            "An ExecutionPlan was submitted via KernelFacade.submit_plan(). "
            "Recorded as an authoritative fact so the event log captures "
            "the full delegation intent before execution begins."
        ),
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.approval_submitted",
        description=(
            "A human-actor approval decision (approved or denied) was received "
            "via KernelFacade.submit_approval(). Recorded as authoritative fact."
        ),
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.speculation_committed",
        description=(
            "A winning speculative candidate was committed via "
            "KernelFacade.commit_speculation(). Triggers cancellation of "
            "all other speculative child runs."
        ),
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.recovery_succeeded",
        description="A recovery action succeeded; run may continue.",
        authority="Recovery",
        affects_replay=True,
    ),
    # --- Heartbeat ---
    EventTypeDescriptor(
        event_type="run.heartbeat",
        description=(
            "Periodic liveness signal emitted by RunHeartbeatMonitor; "
            "never replayed. Used to prove a run is making forward progress."
        ),
        authority="RunHeartbeatMonitor",
        affects_replay=False,
    ),
    EventTypeDescriptor(
        event_type="run.heartbeat_timeout",
        description=(
            "A run exceeded its heartbeat policy timeout. "
            "Injected as a signal by the watchdog; routes to run.recovering "
            "via the canonical signal pathway → Recovery authority."
        ),
        authority="RunHeartbeatMonitor",
        affects_replay=True,
    ),
    # --- Observability (non-replay) ---
    EventTypeDescriptor(
        event_type="derived_diagnostic",
        description="Diagnostic event for observability only; never replayed orused in recovery.",
        authority="ObservabilityHook",
        affects_replay=False,
    ),
    # --- Task lifecycle events (TaskManager layer) ---
    EventTypeDescriptor(
        event_type="task.registered",
        description=(
            "A new task descriptor was registered in TaskRegistry. "
            "Emitted once per task_id at registration time."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.attempt_started",
        description=(
            "A new run attempt was launched for a task. attempt_seq increments on each retry."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.attempt_completed",
        description="A run attempt for a task finished with outcome='completed'.",
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.attempt_failed",
        description=(
            "A run attempt for a task finished with outcome='failed' or 'cancelled'. "
            "RestartPolicyEngine will evaluate next action."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.restarting",
        description=(
            "RestartPolicyEngine decided to retry; new run is being launched. "
            "Task transitions to 'restarting' state."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.reflecting",
        description=(
            "Retry budget exhausted; task is awaiting model reflection decision. "
            "ReflectionBridge will construct LLM context."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="task.completed",
        description=(
            "Task goal achieved; all active attempts are complete. "
            "Terminal state — no further attempts will be made."
        ),
        authority="TaskManager",
        affects_replay=True,
    ),
]

for _descriptor in _KERNEL_EVENTS:
    KERNEL_EVENT_REGISTRY.register(_descriptor)

# TRACE branch + task_view + human_gate lifecycle events
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="branch.opened",
        description="A new trajectory branch was opened within a run (TRACE CTS).",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="branch.state_updated",
        description="Branch lifecycle state transitioned (active/waiting/pruned/succeeded/failed).",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="branch.pruned",
        description="Branch was pruned by route policy or budget exhaustion.",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="branch.succeeded",
        description="Branch reached succeeded terminal state.",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="branch.failed",
        description="Branch reached failed terminal state.",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="task_view.recorded",
        description=(
            "TaskViewRecord persisted; references what context was shown for this decision."
        ),
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="human_gate.opened",
        description="Human review gate opened (Gate A/B/C/D).",
        authority="RunActor",
        affects_replay=True,
    )
)
KERNEL_EVENT_REGISTRY.register(
    EventTypeDescriptor(
        event_type="run.policy_versions_pinned",
        description="Policy versions frozen at run creation for TRACE version pinning.",
        authority="RunActor",
        affects_replay=True,
    )
)


def recovery_allowed_event_types() -> frozenset[str]:
    """Return frozenset of event types permitted in the recovery append path.

    Derived from ``KERNEL_EVENT_REGISTRY`` entries where
    ``recovery_path_allowed=True``.  Use this as the single source of truth
    for ``_assert_recovery_event_type_allowed`` so the allowlist stays in sync
    with the registry without manual duplication.

    Returns:
        Immutable set of event type strings allowed in
        ``_append_recovery_event``.
    """
    return frozenset(d.event_type for d in KERNEL_EVENT_REGISTRY.all() if d.recovery_path_allowed)


def validate_event_type(event_type: str, strict: bool = False) -> bool:
    """Check whether ``event_type`` is registered in ``KERNEL_EVENT_REGISTRY``.

    Dynamic signal event types that follow the ``signal.{name}`` pattern are
    allowed unconditionally because they are constructed at runtime from
    external signal names and cannot be enumerated at import time.

    Args:
        event_type: The event type string to validate.
        strict: When ``True`` raises ``ValueError`` for unknown types so that
            strict-mode deployments prevent ad-hoc event type pollution.
            When ``False`` (default) only emits a WARNING log.

    Returns:
        ``True`` when the event_type is registered or follows the
        ``signal.*`` dynamic pattern.

    Raises:
        ValueError: When ``strict=True`` and the event_type is not registered.
    """
    if event_type in KERNEL_EVENT_REGISTRY.known_types():
        return True
    # signal.{name} event types are dynamically constructed from external
    # signal names and are explicitly permitted in the event taxonomy.
    if event_type.startswith("signal."):
        return True
    msg = (
        f"Event type '{event_type}' is not registered in KERNEL_EVENT_REGISTRY. "
        "Call KERNEL_EVENT_REGISTRY.register() at module import time to prevent "
        "semantic pollution across teams."
    )
    if strict:
        raise ValueError(msg)
    _registry_logger.warning(msg)
    return False


def validate_event_schema_version(schema_version: str, strict: bool = False) -> bool:
    """Check whether *schema_version* matches the current event schema version.

    Used to detect stale deserialized ``RuntimeEvent`` objects from older
    deployments or persisted event logs written by a previous kernel version.

    Dynamic signal events do not carry separate schema versions and are always
    considered compatible.

    Args:
        schema_version: The ``RuntimeEvent.schema_version`` value to validate.
        strict: When ``True`` raises ``ValueError`` for mismatched versions so
            that strict-mode deployments can reject incompatible events at the
            boundary.  When ``False`` (default) only emits a WARNING log.

    Returns:
        ``True`` when *schema_version* matches the current version.

    Raises:
        ValueError: When ``strict=True`` and *schema_version* does not match.
    """
    if schema_version == _CURRENT_EVENT_SCHEMA_VERSION:
        return True
    msg = (
        f"RuntimeEvent schema_version={schema_version!r} does not match "
        f"current version={_CURRENT_EVENT_SCHEMA_VERSION!r}. "
        "Events from older deployments may be missing fields or carry "
        "incompatible payload shapes."
    )
    if strict:
        raise ValueError(msg)
    _registry_logger.warning(msg)
    return False
