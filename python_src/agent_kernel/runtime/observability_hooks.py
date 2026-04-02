"""Built-in ObservabilityHook implementations for agent-kernel.

Provides:
- ``NoOpObservabilityHook``: Pass-through, zero overhead.
- ``LoggingObservabilityHook``: Structured DEBUG logs for each transition.
- ``CompositeObservabilityHook``: Fan-out to multiple hooks.
- ``OtelObservabilityHook``: OpenTelemetry span-per-transition (optional dep).

Usage::

    from agent_kernel.runtime.observability_hooks import (
        LoggingObservabilityHook,
        OtelObservabilityHook,
        CompositeObservabilityHook,
    )

    hook = CompositeObservabilityHook([
        LoggingObservabilityHook(),
        OtelObservabilityHook(),  # no-op when opentelemetry is not installed
    ])

``OtelObservabilityHook`` requires the ``opentelemetry-api`` package.  When the
package is absent the hook silently degrades to a no-op so deployments without
an OTel backend pay zero cost.  When present it emits one span per FSM
transition::

    pip install opentelemetry-api  # add opentelemetry-sdk + exporter for export

Each turn span carries attributes::

    agent_kernel.run_id, agent_kernel.action_id,
    agent_kernel.from_state, agent_kernel.to_state,
    agent_kernel.turn_offset, agent_kernel.timestamp_ms

Each run-lifecycle span carries::

    agent_kernel.run_id,
    agent_kernel.from_state, agent_kernel.to_state,
    agent_kernel.timestamp_ms
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Optional OpenTelemetry import — degrades gracefully when not installed.
# ---------------------------------------------------------------------------
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import NonRecordingSpan as _NonRecordingSpan

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore[assignment]
    _NonRecordingSpan = None  # type: ignore[assignment,misc]


class NoOpObservabilityHook:
    """Observability hook that discards all events.

    Use as default when no observability backend is configured.
    Zero overhead: all methods are no-ops.
    """

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Discard turn state transition event."""

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Discard run lifecycle transition event."""


@dataclass(slots=True)
class LoggingObservabilityHook:
    """Observability hook that emits structured DEBUG log lines.

    Each FSM transition is logged at DEBUG level with all context fields
    as structured key=value pairs, suitable for ingestion by log aggregators.

    Args:
        logger_name: Logger name to emit records on.
            Defaults to ``"agent_kernel.observability"``.
    """

    logger_name: str = "agent_kernel.observability"

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Log turn FSM transition at DEBUG level.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            from_state: Previous FSM state.
            to_state: New FSM state.
            turn_offset: Monotonic turn offset.
            timestamp_ms: UTC epoch milliseconds.
        """
        logging.getLogger(self.logger_name).debug(
            "turn_transition run_id=%s action_id=%s %s->%s offset=%d ts=%d",
            run_id,
            action_id,
            from_state,
            to_state,
            turn_offset,
            timestamp_ms,
        )

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Log run lifecycle transition at DEBUG level.

        Args:
            run_id: Run identifier.
            from_state: Previous lifecycle state.
            to_state: New lifecycle state.
            timestamp_ms: UTC epoch milliseconds.
        """
        logging.getLogger(self.logger_name).debug(
            "run_transition run_id=%s %s->%s ts=%d",
            run_id,
            from_state,
            to_state,
            timestamp_ms,
        )


@dataclass(slots=True)
class CompositeObservabilityHook:
    """Fan-out ObservabilityHook that delegates to multiple inner hooks.

    Use this to attach both a ``LoggingObservabilityHook`` and a
    ``RunHeartbeatMonitor`` without removing either::

        hook = CompositeObservabilityHook([
            LoggingObservabilityHook(),
            RunHeartbeatMonitor(policy),
        ])

    Exceptions raised by any inner hook are swallowed individually so that
    one failing hook never silences the others.

    Args:
        hooks: Ordered list of hook implementations to fan-out to.
    """

    hooks: list[Any] = field(default_factory=list)

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Fan-out turn FSM transition to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_turn_state_transition(
                    run_id=run_id,
                    action_id=action_id,
                    from_state=from_state,
                    to_state=to_state,
                    turn_offset=turn_offset,
                    timestamp_ms=timestamp_ms,
                )

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Fan-out run lifecycle transition to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_run_lifecycle_transition(
                    run_id=run_id,
                    from_state=from_state,
                    to_state=to_state,
                    timestamp_ms=timestamp_ms,
                )


@dataclass(slots=True)
class OtelObservabilityHook:
    """OpenTelemetry-backed ObservabilityHook that emits one span per
    transition.

    Degrades to a no-op when ``opentelemetry-api`` is not installed, so
    deployments without an OTel backend pay zero import cost.

    Each ``on_turn_state_transition`` call produces a child span named
    ``"agent_kernel.turn_transition"`` under the current active trace context.
    Each ``on_run_lifecycle_transition`` call produces a child span named
    ``"agent_kernel.run_transition"``.

    Attributes:
        tracer_name: Instrumentation scope name passed to
            ``opentelemetry.trace.get_tracer()``.
            Defaults to ``"agent_kernel"``.

    Example::

        from agent_kernel.runtime.observability_hooks import
        OtelObservabilityHook

        hook = OtelObservabilityHook()
        # Wire into CompositeObservabilityHook or use directly.
    """

    tracer_name: str = "agent_kernel"

    def _get_tracer(self) -> Any:
        if not _OTEL_AVAILABLE:
            return None
        return _otel_trace.get_tracer(self.tracer_name)

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Emit an OTel span for a TurnEngine FSM state transition.

        The span is a child of whatever trace context is active at call time.
        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            from_state: Previous FSM state.
            to_state: New FSM state.
            turn_offset: Monotonic turn offset.
            timestamp_ms: UTC epoch milliseconds.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span(
            "agent_kernel.turn_transition"
        ) as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.from_state", from_state)
                span.set_attribute("agent_kernel.to_state", to_state)
                span.set_attribute("agent_kernel.turn_offset", turn_offset)
                span.set_attribute("agent_kernel.timestamp_ms", timestamp_ms)

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Emit an OTel span for a run lifecycle state transition.

        The span is a child of whatever trace context is active at call time.
        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            from_state: Previous lifecycle state.
            to_state: New lifecycle state.
            timestamp_ms: UTC epoch milliseconds.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span(
            "agent_kernel.run_transition"
        ) as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.from_state", from_state)
                span.set_attribute("agent_kernel.to_state", to_state)
                span.set_attribute("agent_kernel.timestamp_ms", timestamp_ms)
