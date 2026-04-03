"""Compensation handler registry for static_compensation recovery mode.

Design rationale:
  ``RecoveryMode.static_compensation`` has always existed as a typed mode but
  the kernel had no mechanism to actually *execute* compensation — it could only
  record the intent.  This module closes that gap.

  A ``CompensationRegistry`` maps ``effect_class → async callable`` so the
  recovery path can look up and invoke the appropriate rollback handler when
  ``static_compensation`` is selected.  The registry is injected into
  ``PlannedRecoveryGateService``; if no handler is registered for a failing
  action's ``effect_class``, the gate downgrades the decision to ``abort``
  rather than emitting a compensation decision that can never execute.

Boundary:
  The registry is a *kernel-internal* facility.  Compensation callables receive
  the failed ``Action`` and are responsible for undoing its side effects.  They
  must not mutate kernel event log state directly; any observable state change
  should come through normal action dispatch.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.dedupe_store import DedupeStorePort

from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope

_comp_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompensationEntry:
    """One registered compensation handler.

    Attributes:
        effect_class: The ``EffectClass`` value this handler covers.
        compensate: Async callable that receives the failed ``Action`` and
            executes the rollback.  Must not raise — exceptions should be
            caught internally and logged.
        description: Human-readable description of the compensation strategy.
    """

    effect_class: str
    compensate: Callable[..., Any]
    description: str = ""


class CompensationRegistry:
    """Maps effect_class values to async compensation callables.

    Usage::

        registry = CompensationRegistry()

        @registry.handler("compensatable_write", description="Delete created record")
        async def _undo_write(action: Action) -> None:
            await my_store.delete(action.input_json["record_id"])

        # Or via register():
        registry.register(
            effect_class="compensatable_write",
            compensate=_undo_write,
            description="Delete created record",
        )

    Inject into ``PlannedRecoveryGateService``::

        gate = PlannedRecoveryGateService(compensation_registry=registry)

    The gate will verify that a handler exists before emitting a
    ``static_compensation`` decision; without a handler it falls back to
    ``abort``.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CompensationEntry] = {}

    def register(
        self,
        effect_class: str,
        compensate: Callable[..., Any],
        *,
        description: str = "",
    ) -> None:
        """Registers one compensation handler for an effect class.

        Overwrites any previously registered handler for the same
        ``effect_class``.

        Args:
            effect_class: The ``EffectClass`` string to handle.
            compensate: Async callable accepting one ``Action`` argument.
            description: Optional human-readable description of the strategy.
        """
        self._entries[effect_class] = CompensationEntry(
            effect_class=effect_class,
            compensate=compensate,
            description=description,
        )
        _comp_logger.debug(
            "CompensationRegistry: registered handler effect_class=%s description=%r",
            effect_class,
            description,
        )

    def handler(
        self,
        effect_class: str,
        *,
        description: str = "",
    ) -> Callable[..., Any]:
        """Decorator form of :meth:`register`.

        Usage::

            @registry.handler("compensatable_write", description="Undo create")
            async def _undo(action: Action) -> None:
                ...

        Args:
            effect_class: The ``EffectClass`` string to handle.
            description: Optional human-readable description.

        Returns:
            Decorator that registers the wrapped callable and returns it
            unchanged.
        """

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(effect_class, fn, description=description)
            return fn

        return _decorator

    def lookup(self, effect_class: str) -> CompensationEntry | None:
        """Returns the registered handler for an effect class, or ``None``.

        Args:
            effect_class: The ``EffectClass`` string to look up.

        Returns:
            Registered ``CompensationEntry``, or ``None`` when not found.
        """
        return self._entries.get(effect_class)

    def has_handler(self, effect_class: str) -> bool:
        """Returns whether a handler is registered for the given effect class.

        Args:
            effect_class: The ``EffectClass`` string to check.

        Returns:
            ``True`` when a handler is registered.
        """
        return effect_class in self._entries

    def registered_effect_classes(self) -> list[str]:
        """Returns sorted list of effect classes with registered handlers.

        Returns:
            Sorted list of registered ``EffectClass`` strings.
        """
        return sorted(self._entries.keys())

    async def execute(
        self,
        action: Any,
        *,
        dedupe_store: DedupeStorePort | None = None,
        run_id: str | None = None,
    ) -> bool:
        """Executes the registered compensation handler for an action.

        Looks up the handler by ``action.effect_class`` and calls it.
        Returns ``False`` when no handler is registered (caller should
        escalate or abort).

        When *dedupe_store* is provided, each compensation attempt is wrapped
        with an ``IdempotencyEnvelope`` for at-most-once execution.  If the
        idempotency slot is already reserved (e.g. from a prior attempt in
        the same recovery round), the handler is not called again and the
        method returns ``True`` (already compensated = idempotent success).

        The idempotency key is deterministically derived from the action
        identity and does not depend on wall-clock time or random values::

            compensation:{effect_class}:{action_id}

        Args:
            action: The failed ``Action`` whose side effect needs undoing.
            dedupe_store: Optional ``DedupeStorePort`` for at-most-once
                execution.  When ``None`` the handler is called without
                idempotency protection (backward-compatible default).
            run_id: Optional run identifier used for logging context.

        Returns:
            ``True`` when a handler was found and executed (or already
            compensated), ``False`` when no handler is registered.
        """
        entry = self._entries.get(action.effect_class)
        if entry is None:
            _comp_logger.warning(
                "CompensationRegistry: no handler for effect_class=%s action_id=%s",
                action.effect_class,
                action.action_id,
            )
            return False

        idempotency_key: str | None = None
        if dedupe_store is not None:
            idempotency_key = f"compensation:{action.effect_class}:{action.action_id}"
            envelope = IdempotencyEnvelope(
                dispatch_idempotency_key=idempotency_key,
                operation_fingerprint=idempotency_key,
                attempt_seq=1,
                effect_scope=action.effect_class,
                capability_snapshot_hash="compensation",
                host_kind="local_process",
            )
            reservation = dedupe_store.reserve(envelope)
            if not reservation.accepted:
                _comp_logger.info(
                    "CompensationRegistry: skipped (already reserved) "
                    "effect_class=%s action_id=%s run_id=%s key=%s",
                    action.effect_class,
                    action.action_id,
                    run_id,
                    idempotency_key,
                )
                return True
            dedupe_store.mark_dispatched(idempotency_key)

        try:
            await entry.compensate(action)
            _comp_logger.info(
                "CompensationRegistry: compensation executed effect_class=%s action_id=%s",
                action.effect_class,
                action.action_id,
            )
            if dedupe_store is not None and idempotency_key is not None:
                dedupe_store.mark_acknowledged(idempotency_key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _comp_logger.error(
                "CompensationRegistry: handler raised effect_class=%s action_id=%s error=%r",
                action.effect_class,
                action.action_id,
                exc,
            )
            if dedupe_store is not None and idempotency_key is not None:
                with contextlib.suppress(Exception):
                    dedupe_store.mark_unknown_effect(idempotency_key)
            return False
        return True
