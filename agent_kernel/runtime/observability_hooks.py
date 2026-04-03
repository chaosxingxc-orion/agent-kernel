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
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Optional OpenTelemetry import — degrades gracefully when not installed.
# ---------------------------------------------------------------------------
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import NonRecordingSpan as _NonRecordingSpan
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator as _TraceContextPropagator,
    )

    _OTEL_AVAILABLE = True
    _PROPAGATOR = _TraceContextPropagator()
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore[assignment]
    _NonRecordingSpan = None  # type: ignore[assignment,misc]
    _PROPAGATOR = None  # type: ignore[assignment]


def _extract_otel_context(trace_context: str | None) -> Any | None:
    """Extracts OTel context from a W3C traceparent string.

    Returns ``None`` (ambient context) when OTel is not installed, when
    ``trace_context`` is absent, or when extraction fails.

    Args:
        trace_context: W3C traceparent string or ``None``.

    Returns:
        Extracted OTel ``Context`` for use as a span parent, or ``None``.
    """
    if not _OTEL_AVAILABLE or not trace_context or _PROPAGATOR is None:
        return None
    try:
        return _PROPAGATOR.extract({"traceparent": trace_context})
    except Exception:  # pylint: disable=broad-exception-caught  # pragma: no cover
        return None


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

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Discard LLM call event."""

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Discard action dispatch event."""

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Discard recovery triggered event."""

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Discard branch rollback triggered event."""

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Discard turn phase event."""


def _json_log(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a single-line JSON log record at DEBUG level.

    The record always contains ``ts_ms`` (Unix epoch ms), ``event``, and any
    extra keyword fields passed in.  Using plain ``json.dumps`` avoids a
    structlog/pythonjsonlogger dependency.

    Args:
        logger: Target logger.
        event: Short event name (e.g. ``"turn_transition"``).
        **fields: Additional context fields serialised into the JSON object.
    """
    record = {"ts_ms": int(time.time() * 1_000), "event": event, **fields}
    logger.debug(json.dumps(record, default=str))


@dataclass(slots=True)
class LoggingObservabilityHook:
    """Observability hook that emits structured DEBUG log lines.

    Supports two output formats:

    - **key=value** (default, ``use_json=False``): human-readable lines suited
      for local development and legacy log shippers.
    - **JSON** (``use_json=True``): one JSON object per line, suitable for
      Elasticsearch, Loki, Datadog, and other structured-log ingestion systems.
      Each record includes ``ts_ms`` (Unix epoch milliseconds), ``event``, and
      all hook context fields.

    Args:
        logger_name: Logger name to emit records on.
            Defaults to ``"agent_kernel.observability"``.
        use_json: When ``True`` emits JSON-formatted log lines.
            Defaults to ``False`` (key=value text format).
    """

    logger_name: str = "agent_kernel.observability"
    use_json: bool = False

    def _log(self, event: str, **fields: Any) -> None:
        """Emit one log record in the configured format."""
        logger = logging.getLogger(self.logger_name)
        if self.use_json:
            _json_log(logger, event, **fields)
        else:
            parts = " ".join(f"{k}={v}" for k, v in fields.items())
            logger.debug("%s %s", event, parts)

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
        self._log(
            "turn_transition",
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
        """Log run lifecycle transition at DEBUG level.

        Args:
            run_id: Run identifier.
            from_state: Previous lifecycle state.
            to_state: New lifecycle state.
            timestamp_ms: UTC epoch milliseconds.
        """
        self._log(
            "run_transition",
            run_id=run_id,
            from_state=from_state,
            to_state=to_state,
            timestamp_ms=timestamp_ms,
        )

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Log LLM call at DEBUG level."""
        tok_in = getattr(token_usage, "input_tokens", 0) if token_usage else 0
        tok_out = getattr(token_usage, "output_tokens", 0) if token_usage else 0
        self._log(
            "llm_call",
            run_id=run_id,
            model_ref=model_ref,
            latency_ms=latency_ms,
            tok_in=tok_in,
            tok_out=tok_out,
        )

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Log action dispatch at DEBUG level."""
        self._log(
            "action_dispatch",
            run_id=run_id,
            action_id=action_id,
            action_type=action_type,
            outcome_kind=outcome_kind,
            latency_ms=latency_ms,
        )

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Log recovery triggered at DEBUG level."""
        self._log(
            "recovery_triggered",
            run_id=run_id,
            reason_code=reason_code,
            mode=mode,
        )

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Log admission evaluation at DEBUG level."""
        self._log(
            "admission_evaluated",
            run_id=run_id,
            action_id=action_id,
            admitted=admitted,
            latency_ms=latency_ms,
        )

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Log dispatch attempt at DEBUG level."""
        self._log(
            "dispatch_attempted",
            run_id=run_id,
            action_id=action_id,
            dedupe_outcome=dedupe_outcome,
            latency_ms=latency_ms,
        )

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Log parallel branch result at DEBUG level."""
        self._log(
            "branch_result",
            run_id=run_id,
            group_idempotency_key=group_idempotency_key,
            action_id=action_id,
            outcome=outcome,
            failure_code=failure_code,
        )

    def on_dedupe_hit(self, *, run_id: str, action_id: str, outcome: str) -> None:
        """Log DedupeStore reservation outcome."""
        self._log("dedupe_hit", run_id=run_id, action_id=action_id, outcome=outcome)

    def on_reflection_round(
        self, *, run_id: str, action_id: str, round_num: int, corrected: bool
    ) -> None:
        """Log reflection loop round completion."""
        self._log(
            "reflection_round",
            run_id=run_id,
            action_id=action_id,
            round_num=round_num,
            corrected=corrected,
        )

    def on_circuit_breaker_trip(
        self, *, run_id: str, effect_class: str, failure_count: int, tripped: bool
    ) -> None:
        """Log circuit breaker state change."""
        self._log(
            "circuit_breaker_trip",
            run_id=run_id,
            effect_class=effect_class,
            failure_count=failure_count,
            tripped=tripped,
        )

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Log branch rollback intent at DEBUG level."""
        self._log(
            "branch_rollback_triggered",
            run_id=run_id,
            group_idempotency_key=group_idempotency_key,
            action_id=action_id,
            join_strategy=join_strategy,
        )

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Log turn phase completion at DEBUG level."""
        self._log(
            "turn_phase",
            run_id=run_id,
            action_id=action_id,
            phase_name=phase_name,
            elapsed_ms=elapsed_ms,
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

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Fan-out LLM call event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_llm_call(
                    run_id=run_id,
                    model_ref=model_ref,
                    latency_ms=latency_ms,
                    token_usage=token_usage,
                )

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Fan-out action dispatch event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_action_dispatch(
                    run_id=run_id,
                    action_id=action_id,
                    action_type=action_type,
                    outcome_kind=outcome_kind,
                    latency_ms=latency_ms,
                )

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Fan-out recovery triggered event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_recovery_triggered(
                    run_id=run_id,
                    reason_code=reason_code,
                    mode=mode,
                )

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Fan-out admission evaluation event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_admission_evaluated(
                    run_id=run_id,
                    action_id=action_id,
                    admitted=admitted,
                    latency_ms=latency_ms,
                )

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Fan-out dispatch attempted event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_dispatch_attempted(
                    run_id=run_id,
                    action_id=action_id,
                    dedupe_outcome=dedupe_outcome,
                    latency_ms=latency_ms,
                )

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Fan-out parallel branch result event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_parallel_branch_result(
                    run_id=run_id,
                    group_idempotency_key=group_idempotency_key,
                    action_id=action_id,
                    outcome=outcome,
                    failure_code=failure_code,
                )

    def on_dedupe_hit(self, *, run_id: str, action_id: str, outcome: str) -> None:
        """Fan-out dedupe hit event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_dedupe_hit(run_id=run_id, action_id=action_id, outcome=outcome)

    def on_reflection_round(
        self, *, run_id: str, action_id: str, round_num: int, corrected: bool
    ) -> None:
        """Fan-out reflection round event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_reflection_round(
                    run_id=run_id, action_id=action_id,
                    round_num=round_num, corrected=corrected,
                )

    def on_circuit_breaker_trip(
        self, *, run_id: str, effect_class: str, failure_count: int, tripped: bool
    ) -> None:
        """Fan-out circuit breaker trip event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_circuit_breaker_trip(
                    run_id=run_id, effect_class=effect_class,
                    failure_count=failure_count, tripped=tripped,
                )

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Fan-out branch rollback triggered event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_branch_rollback_triggered(
                    run_id=run_id,
                    group_idempotency_key=group_idempotency_key,
                    action_id=action_id,
                    join_strategy=join_strategy,
                )

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Fan-out turn phase event to all inner hooks."""
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                hook.on_turn_phase(
                    run_id=run_id,
                    action_id=action_id,
                    phase_name=phase_name,
                    elapsed_ms=elapsed_ms,
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
        trace_context: str | None = None,
    ) -> None:
        """Emit an OTel span for a TurnEngine FSM state transition.

        When ``trace_context`` is a valid W3C ``traceparent`` string, the span
        is created as a child of the referenced remote trace so that cross-service
        calls appear in a single distributed trace.  Falls back to the current
        ambient context when ``trace_context`` is absent or invalid.
        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            from_state: Previous FSM state.
            to_state: New FSM state.
            turn_offset: Monotonic turn offset.
            timestamp_ms: UTC epoch milliseconds.
            trace_context: Optional W3C traceparent to restore remote parent span.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        context = _extract_otel_context(trace_context)
        with tracer.start_as_current_span(
            "agent_kernel.turn_transition",
            context=context,
        ) as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.from_state", from_state)
                span.set_attribute("agent_kernel.to_state", to_state)
                span.set_attribute("agent_kernel.turn_offset", turn_offset)
                span.set_attribute("agent_kernel.timestamp_ms", timestamp_ms)
                if trace_context:
                    span.set_attribute("agent_kernel.trace_context", trace_context)

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

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Emit an OTel span for an LLM inference call.

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            model_ref: Provider-qualified model identifier.
            latency_ms: Wall-clock latency in milliseconds.
            token_usage: Typed token consumption, or ``None``.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.llm_call") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.model_ref", model_ref)
                span.set_attribute("agent_kernel.latency_ms", latency_ms)
                if token_usage is not None:
                    span.set_attribute(
                        "agent_kernel.token_usage.input", token_usage.input_tokens
                    )
                    span.set_attribute(
                        "agent_kernel.token_usage.output", token_usage.output_tokens
                    )
                    span.set_attribute(
                        "agent_kernel.token_usage.reasoning",
                        token_usage.reasoning_tokens,
                    )

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Emit an OTel span for an action dispatch attempt.

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            action_type: Discriminator string for the action class.
            outcome_kind: Outcome label (``"dispatched"``, ``"blocked"``, etc.).
            latency_ms: Wall-clock latency of the dispatch call.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.action_dispatch") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.action_type", action_type)
                span.set_attribute("agent_kernel.outcome_kind", outcome_kind)
                span.set_attribute("agent_kernel.latency_ms", latency_ms)

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Emit an OTel span for a recovery decision.

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            reason_code: Failure reason code that triggered recovery.
            mode: Recovery mode selected.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.recovery_triggered") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.reason_code", reason_code)
                span.set_attribute("agent_kernel.recovery_mode", mode)

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Emit an OTel span for an admission gate evaluation.

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            action_id: Action being admitted or rejected.
            admitted: ``True`` when admission was granted.
            latency_ms: Wall-clock duration of the admission check.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.admission") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.admitted", admitted)
                span.set_attribute("agent_kernel.latency_ms", latency_ms)

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Emit an OTel span for a dispatch attempt (reservation + executor).

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            action_id: Action being dispatched.
            dedupe_outcome: Outcome of the dedupe reservation.
            latency_ms: Wall-clock duration from reservation to executor return.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.dispatch") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.dedupe_outcome", dedupe_outcome)
                span.set_attribute("agent_kernel.latency_ms", latency_ms)

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Emit an OTel span for a parallel branch completion.

        When OTel is not installed this method is a no-op.

        Args:
            run_id: Run identifier.
            group_idempotency_key: Stable key for the parallel group.
            action_id: Branch action identifier.
            outcome: Branch outcome label.
            failure_code: Exception type or failure discriminator when failed.
        """
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.branch_result") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.group_key", group_idempotency_key)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.outcome", outcome)
                if failure_code is not None:
                    span.set_attribute("agent_kernel.failure_code", failure_code)

    def on_dedupe_hit(self, *, run_id: str, action_id: str, outcome: str) -> None:
        """Emit an OTel span for a DedupeStore reservation attempt."""
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.dedupe_hit") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.outcome", outcome)

    def on_reflection_round(
        self, *, run_id: str, action_id: str, round_num: int, corrected: bool
    ) -> None:
        """Emit an OTel span for one reflection loop round."""
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.reflection_round") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.round_num", round_num)
                span.set_attribute("agent_kernel.corrected", corrected)

    def on_circuit_breaker_trip(
        self, *, run_id: str, effect_class: str, failure_count: int, tripped: bool
    ) -> None:
        """Emit an OTel span for a circuit breaker state change."""
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span("agent_kernel.circuit_breaker_trip") as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.effect_class", effect_class)
                span.set_attribute("agent_kernel.failure_count", failure_count)
                span.set_attribute("agent_kernel.tripped", tripped)

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Emit an OTel span for a branch rollback intent signal."""
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span(
            "agent_kernel.branch_rollback_triggered"
        ) as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.group_key", group_idempotency_key)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.join_strategy", join_strategy)

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Emit an OTel sub-span for one TurnEngine phase."""
        tracer = self._get_tracer()
        if tracer is None:
            return
        with tracer.start_as_current_span(
            f"agent_kernel.phase.{phase_name.lstrip('_')}"
        ) as span:
            if span.is_recording():
                span.set_attribute("agent_kernel.run_id", run_id)
                span.set_attribute("agent_kernel.action_id", action_id)
                span.set_attribute("agent_kernel.phase_name", phase_name)
                span.set_attribute("agent_kernel.elapsed_ms", elapsed_ms)


# ---------------------------------------------------------------------------
# MetricsObservabilityHook — OTel Counter + Histogram metrics
# ---------------------------------------------------------------------------

try:
    from opentelemetry import metrics as _otel_metrics  # type: ignore[attr-defined]

    _OTEL_METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_METRICS_AVAILABLE = False
    _otel_metrics = None  # type: ignore[assignment]


class MetricsObservabilityHook:
    """ObservabilityHook that records OTel Counter and Histogram metrics.

    All OTel instruments are created once in ``__init__`` and reused on every
    call — creating instruments per-call is an OTel API misuse and wastes CPU.

    Instruments created (all ``None`` when ``opentelemetry-api`` is absent):

    * ``agent_kernel.llm_calls`` (Counter) — per LLM call, keyed by
      ``model_ref``.
    * ``agent_kernel.llm_latency_ms`` (Histogram) — wall-clock inference
      latency in milliseconds.
    * ``agent_kernel.llm_input_tokens`` / ``agent_kernel.llm_output_tokens``
      (Counter) — token consumption totals.
    * ``agent_kernel.action_dispatches`` (Counter) — per dispatch attempt,
      keyed by ``action_type`` and ``outcome_kind``.
    * ``agent_kernel.action_dispatch_latency_ms`` (Histogram) — dispatch
      latency in milliseconds.
    * ``agent_kernel.recovery_triggers`` (Counter) — per recovery decision,
      keyed by ``mode`` and ``reason_code``.

    Degrades to a no-op when ``opentelemetry-api`` is not installed.

    Args:
        meter_name: Instrumentation scope name passed to
            ``opentelemetry.metrics.get_meter()``.
            Defaults to ``"agent_kernel"``.
    """

    def __init__(self, meter_name: str = "agent_kernel") -> None:
        self.meter_name = meter_name
        self._llm_calls: Any = None
        self._llm_latency: Any = None
        self._llm_input_tokens: Any = None
        self._llm_output_tokens: Any = None
        self._action_dispatches: Any = None
        self._action_dispatch_latency: Any = None
        self._recovery_triggers: Any = None
        self._parallel_branch_results: Any = None
        self._dedupe_hits: Any = None
        self._reflection_rounds: Any = None
        self._circuit_breaker_trips: Any = None
        self._branch_rollbacks: Any = None
        self._turn_phase_latency: Any = None
        self._admission_evaluations: Any = None
        self._dispatch_attempt_latency: Any = None
        if _OTEL_METRICS_AVAILABLE and _otel_metrics is not None:
            meter = _otel_metrics.get_meter(meter_name)
            self._llm_calls = meter.create_counter(
                "agent_kernel.llm_calls",
                description="Number of LLM inference calls",
            )
            self._llm_latency = meter.create_histogram(
                "agent_kernel.llm_latency_ms",
                description="Wall-clock LLM inference latency in milliseconds",
                unit="ms",
            )
            self._llm_input_tokens = meter.create_counter(
                "agent_kernel.llm_input_tokens",
                description="Total input tokens consumed by LLM inference calls",
            )
            self._llm_output_tokens = meter.create_counter(
                "agent_kernel.llm_output_tokens",
                description="Total output tokens produced by LLM inference calls",
            )
            self._action_dispatches = meter.create_counter(
                "agent_kernel.action_dispatches",
                description="Number of action dispatch attempts",
            )
            self._action_dispatch_latency = meter.create_histogram(
                "agent_kernel.action_dispatch_latency_ms",
                description="Wall-clock action dispatch latency in milliseconds",
                unit="ms",
            )
            self._recovery_triggers = meter.create_counter(
                "agent_kernel.recovery_triggers",
                description="Number of recovery decisions triggered",
            )
            self._parallel_branch_results = meter.create_counter(
                "agent_kernel.parallel_branch_results",
                description="Number of parallel branch completions keyed by outcome",
            )
            self._dedupe_hits = meter.create_counter(
                "agent_kernel.dedupe_hits",
                description="DedupeStore reservation attempts keyed by outcome",
            )
            self._reflection_rounds = meter.create_counter(
                "agent_kernel.reflection_rounds",
                description="Reflection loop rounds keyed by corrected status",
            )
            self._circuit_breaker_trips = meter.create_counter(
                "agent_kernel.circuit_breaker_trips",
                description="Circuit breaker state changes keyed by effect_class and tripped",
            )
            self._branch_rollbacks = meter.create_counter(
                "agent_kernel.branch_rollbacks",
                description="Branch rollback intent signals keyed by join_strategy",
            )
            self._turn_phase_latency = meter.create_histogram(
                "agent_kernel.turn_phase_latency_ms",
                description="Wall-clock duration per TurnEngine phase in milliseconds",
                unit="ms",
            )
            self._admission_evaluations = meter.create_counter(
                "agent_kernel.admission_evaluations",
                description="Admission gate evaluation counts keyed by admitted status",
            )
            self._dispatch_attempt_latency = meter.create_histogram(
                "agent_kernel.dispatch_attempt_latency_ms",
                description=(
                    "Wall-clock duration from DedupeStore reservation to executor return"
                ),
                unit="ms",
            )

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
        """No-op — FSM transition metrics not tracked here."""

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """No-op — lifecycle transition metrics not tracked here."""

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Record LLM call counter and latency histogram.

        Args:
            run_id: Run identifier.
            model_ref: Provider-qualified model identifier.
            latency_ms: Wall-clock latency in milliseconds.
            token_usage: Typed token consumption, or ``None``.
        """
        if self._llm_calls is None:
            return
        attrs = {"model_ref": model_ref, "run_id": run_id}
        self._llm_calls.add(1, attrs)
        self._llm_latency.record(latency_ms, attrs)
        if token_usage is not None:
            self._llm_input_tokens.add(token_usage.input_tokens, attrs)
            self._llm_output_tokens.add(token_usage.output_tokens, attrs)

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Record action dispatch counter and latency histogram.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            action_type: Discriminator string for the action class.
            outcome_kind: Outcome label.
            latency_ms: Wall-clock latency of the dispatch call.
        """
        if self._action_dispatches is None:
            return
        attrs = {"action_type": action_type, "outcome_kind": outcome_kind}
        self._action_dispatches.add(1, attrs)
        self._action_dispatch_latency.record(latency_ms, attrs)

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Record recovery trigger counter keyed by mode.

        Args:
            run_id: Run identifier.
            reason_code: Failure reason code that triggered recovery.
            mode: Recovery mode selected.
        """
        if self._recovery_triggers is None:
            return
        attrs = {"mode": mode, "reason_code": reason_code}
        self._recovery_triggers.add(1, attrs)

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Record admission evaluation counter keyed by admitted status."""
        if self._admission_evaluations is None:
            return
        self._admission_evaluations.add(1, {"admitted": str(admitted).lower()})

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Record dispatch attempt latency histogram keyed by dedupe_outcome."""
        if self._dispatch_attempt_latency is None:
            return
        self._dispatch_attempt_latency.record(
            latency_ms, {"dedupe_outcome": dedupe_outcome}
        )

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Record parallel branch result counter keyed by outcome.

        Args:
            run_id: Run identifier.
            group_idempotency_key: Stable key for the parallel group.
            action_id: Branch action identifier.
            outcome: Branch outcome label (``"acknowledged"``, ``"failed"``,
                ``"timeout"``).
            failure_code: Exception type or failure discriminator when failed.
        """
        if self._parallel_branch_results is None:
            return
        attrs: dict[str, str] = {"outcome": outcome}
        if failure_code is not None:
            attrs["failure_code"] = failure_code
        self._parallel_branch_results.add(1, attrs)

    def on_dedupe_hit(self, *, run_id: str, action_id: str, outcome: str) -> None:
        """Record DedupeStore reservation counter keyed by outcome."""
        if self._dedupe_hits is None:
            return
        self._dedupe_hits.add(1, {"outcome": outcome})

    def on_reflection_round(
        self, *, run_id: str, action_id: str, round_num: int, corrected: bool
    ) -> None:
        """Record reflection round counter keyed by corrected status."""
        if self._reflection_rounds is None:
            return
        self._reflection_rounds.add(1, {"corrected": str(corrected).lower()})

    def on_circuit_breaker_trip(
        self, *, run_id: str, effect_class: str, failure_count: int, tripped: bool
    ) -> None:
        """Record circuit breaker trip counter keyed by effect_class and tripped status."""
        if self._circuit_breaker_trips is None:
            return
        self._circuit_breaker_trips.add(
            1, {"effect_class": effect_class, "tripped": str(tripped).lower()}
        )

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Record branch rollback intent counter keyed by join_strategy."""
        if self._branch_rollbacks is None:
            return
        self._branch_rollbacks.add(1, {"join_strategy": join_strategy})

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Record per-phase latency histogram keyed by phase_name."""
        if self._turn_phase_latency is None:
            return
        self._turn_phase_latency.record(elapsed_ms, {"phase_name": phase_name})
