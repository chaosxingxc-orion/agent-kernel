"""OpenTelemetry EventExportPort implementation for agent-kernel.

Maps each ``ActionCommit`` from the kernel event log to one OpenTelemetry span
so that agent turns appear as structured traces in Jaeger, Honeycomb, Datadog,
or any OTLP-compatible backend.

Span structure
--------------
Each ``ActionCommit`` becomes one span::

    kernel.turn          (when commit carries an action)
    kernel.lifecycle     (when commit contains only lifecycle events)

Span attributes (on every span)::

    kernel.run_id
    kernel.commit_id
    kernel.action_id         (when action present)
    kernel.action_type       (when action present)
    kernel.effect_class      (when action present)
    kernel.interaction_target (when action present and field is set)

Span events::

    One OTel event per ``RuntimeEvent`` in the commit, named by ``event_type``
    with attributes ``event_authority``, ``wake_policy``, ``commit_offset``.

Optional dependency
-------------------
``opentelemetry-sdk`` is **not** a hard dependency of agent-kernel.  When the
package is not installed this module raises ``ImportError`` only at
instantiation time, not at import time.  Tests can supply any object that
satisfies the ``TracerProvider`` duck type.

Usage::

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    exporter = OTLPRunTraceExporter(tracer_provider=provider)
    config = KernelRuntimeConfig(event_export_port=exporter)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import ActionCommit

_otel_logger = logging.getLogger(__name__)

_SPAN_KIND_INTERNAL = None  # resolved lazily from opentelemetry.trace


class OTLPRunTraceExporter:
    """``EventExportPort`` implementation that emits OpenTelemetry spans.

    Each ``ActionCommit`` becomes one span.  Span events record individual
    ``RuntimeEvent`` entries so the full FSM trace is visible in any
    OTLP-compatible backend.

    Args:
        tracer_provider: An OpenTelemetry ``TracerProvider`` instance.
            When ``None`` the global provider from ``opentelemetry.trace``
            is used.  For testing, pass any object that exposes
            ``get_tracer(name) -> tracer`` where ``tracer.start_as_current_span``
            follows the OTel context-manager API.
        service_name: Tracer instrument scope name.  Appears as
            ``instrumentation_library.name`` in trace backends.
        include_payload: When ``True``, ``payload_json`` of each
            ``RuntimeEvent`` is serialised into span event attributes.
            Disable in production if payloads contain PII.

    """

    def __init__(
        self,
        tracer_provider: Any | None = None,
        *,
        service_name: str = "agent-kernel",
        include_payload: bool = False,
    ) -> None:
        """Initialize the instance with configured dependencies."""
        self._tracer = _resolve_tracer(tracer_provider, service_name)
        self._include_payload = include_payload

    async def export_commit(self, commit: ActionCommit) -> None:
        """Export one commit as an OpenTelemetry span.

        Args:
            commit: The ``ActionCommit`` to export.

        """
        span_name = "kernel.turn" if commit.action is not None else "kernel.lifecycle"
        attributes = _build_span_attributes(commit)

        # Timestamps: use commit created_at if parseable, else current time.
        start_ns = _iso_to_ns(commit.created_at)

        with self._tracer.start_as_current_span(
            span_name,
            attributes=attributes,
            start_time=start_ns,
        ) as span:
            for event in commit.events:
                event_attrs: dict[str, Any] = {
                    "event_authority": event.event_authority,
                    "wake_policy": event.wake_policy,
                    "commit_offset": event.commit_offset,
                    "event_class": event.event_class,
                }
                if self._include_payload and event.payload_json:
                    # Flatten one level of payload as string attributes.
                    for k, v in event.payload_json.items():
                        event_attrs[f"payload.{k}"] = str(v)
                span.add_event(event.event_type, attributes=event_attrs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_tracer(provider: Any | None, service_name: str) -> Any:
    """Resolve an OTel Tracer from the given or global provider.

    Args:
        provider: Explicit ``TracerProvider``, or ``None`` to use global.
        service_name: Tracer scope name.

    Returns:
        An OTel ``Tracer`` object.

    Raises:
        ImportError: If ``opentelemetry-api`` is not installed.

    """
    try:
        import opentelemetry.trace as otel_trace  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "opentelemetry-api is required for OTLPRunTraceExporter. "
            "Install with: pip install opentelemetry-api opentelemetry-sdk"
        ) from exc

    if provider is None:
        provider = otel_trace.get_tracer_provider()
    return provider.get_tracer(service_name, schema_url="https://opentelemetry.io/schemas/1.24.0")


def _build_span_attributes(commit: ActionCommit) -> dict[str, Any]:
    """Build the OTel span attribute dict for a commit.

    Args:
        commit: The commit to extract attributes from.

    Returns:
        Flat dict of OTel-compatible attribute key/value pairs.

    """
    attrs: dict[str, Any] = {
        "kernel.run_id": commit.run_id,
        "kernel.commit_id": commit.commit_id,
        "kernel.event_count": len(commit.events),
    }
    if commit.action is not None:
        action = commit.action
        attrs["kernel.action_id"] = action.action_id
        attrs["kernel.action_type"] = action.action_type
        attrs["kernel.effect_class"] = action.effect_class
        if action.interaction_target is not None:
            attrs["kernel.interaction_target"] = action.interaction_target
        if action.external_idempotency_level is not None:
            attrs["kernel.external_idempotency_level"] = action.external_idempotency_level
    if commit.caused_by is not None:
        attrs["kernel.caused_by"] = commit.caused_by
    return attrs


def _iso_to_ns(iso_timestamp: str) -> int | None:
    """Convert an ISO8601 UTC timestamp string to nanoseconds since epoch.

    Returns ``None`` on parse failure so the span uses the current time.

    Args:
        iso_timestamp: RFC3339 / ISO8601 UTC string ending in 'Z' or '+00:00'.

    Returns:
        Nanoseconds since Unix epoch, or ``None`` on failure.

    """
    try:
        from datetime import UTC, datetime

        ts = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return int(dt.replace(tzinfo=UTC).timestamp() * 1e9)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
