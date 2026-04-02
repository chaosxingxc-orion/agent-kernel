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
    """

    event_type: str
    description: str
    authority: str
    affects_replay: bool = True
    recovery_path_allowed: bool = False


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
        description="RunActor has accepted the run and entered active"
            "lifecycle.",
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
        description="A child run signalled its completion back to this parent"
            "run.",
        authority="RunActor",
        affects_replay=True,
    ),
    # --- Turn / TurnEngine FSM ---
    EventTypeDescriptor(
        event_type="turn.intent_committed",
        description="TurnEngine committed the action intent; FSM advanced to"
            "intent_committed.",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.snapshot_built",
        description="CapabilitySnapshot was deterministically built and hashed."
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.admission_checked",
        description="Admission gate evaluated the snapshot and approved"
            "dispatch.",
        authority="Admission",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.dispatch_blocked",
        description="Admission gate blocked dispatch; action will not be"
            "executed.",
        authority="Admission",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.dispatched",
        description="DedupeStore recorded dispatch reservation; Executor"
            "called.",
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
        description="Executor effect was confirmed and written to"
            "RuntimeEventLog.",
        authority="Executor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.completed_noop",
        description="Turn completed with no external effect (e.g. admission"
            "blocked, noop action).",
        authority="TurnEngine",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="turn.recovery_pending",
        description="Recovery is pending for this turn; outcome not yet"
            "determined.",
        authority="Recovery",
        affects_replay=True,
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
        description="Recovery authority recorded a final outcome for a failed"
            "turn.",
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
        description="A signal was received by the RunActor from an external"
            "caller.",
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
        description="Run entered dispatching state; an action was admitted and"
            "dispatched.",
        authority="RunActor",
        affects_replay=True,
    ),
    EventTypeDescriptor(
        event_type="run.recovering",
        description="Run entered recovery state; a failed action is being"
            "handled.",
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
        description="Recovery decided to abort the run; terminal state follows."
        authority="Recovery",
        affects_replay=True,
        recovery_path_allowed=True,
    ),
    EventTypeDescriptor(
        event_type="run.waiting_external",
        description="Run is paused waiting for an external event or human"
            "review.",
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
        description="Diagnostic event for observability only; never replayed or"
            "used in recovery.",
        authority="ObservabilityHook",
        affects_replay=False,
    ),
]

for _descriptor in _KERNEL_EVENTS:
    KERNEL_EVENT_REGISTRY.register(_descriptor)


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
    return frozenset(
        d.event_type
        for d in KERNEL_EVENT_REGISTRY.all()
        if d.recovery_path_allowed
    )


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
        f"Event type '{event_type}' is not registered in 
            KERNEL_EVENT_REGISTRY. "
        "Call KERNEL_EVENT_REGISTRY.register() at module import time to prevent "
        "semantic pollution across teams."
    )
    if strict:
        raise ValueError(msg)
    _registry_logger.warning(msg)
    return False
