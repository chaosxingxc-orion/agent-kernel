"""Tests for OTLPRunTraceExporter.

Uses a minimal mock tracer so opentelemetry-sdk is NOT required.
All observable behaviour is verified against the mock's recorded calls.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_kernel.kernel.contracts import Action, ActionCommit, RuntimeEvent

# ---------------------------------------------------------------------------
# Minimal mock OTel tracer infrastructure (no sdk dependency)
# ---------------------------------------------------------------------------


@dataclass
class RecordedEvent:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[RecordedEvent] = field(default_factory=list)
    start_time: int | None = None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(RecordedEvent(name=name, attributes=attributes or {}))


class MockTracer:
    """Minimal OTel Tracer mock that records spans without any SDK dependency."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
        start_time: int | None = None,
    ):
        span = RecordedSpan(
            name=name,
            attributes=attributes or {},
            start_time=start_time,
        )
        self.spans.append(span)
        yield span


class MockTracerProvider:
    def __init__(self) -> None:
        self.tracer = MockTracer()

    def get_tracer(self, name: str, **kwargs: Any) -> MockTracer:
        return self.tracer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _make_event(
    run_id: str,
    offset: int,
    event_type: str,
    event_authority: str = "authoritative_fact",
    payload_json: dict[str, Any] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        run_id=run_id,
        event_id=f"evt-{offset}",
        commit_offset=offset,
        event_type=event_type,
        event_class="fact",
        event_authority=event_authority,
        ordering_key=run_id,
        wake_policy="wake_actor",
        created_at=_now(),
        payload_json=payload_json,
    )


def _make_action(
    run_id: str,
    effect_class: str = "idempotent_write",
    action_type: str = "tool_call",
    interaction_target: str | None = None,
) -> Action:
    return Action(
        action_id=f"act-{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        action_type=action_type,
        effect_class=effect_class,  # type: ignore[arg-type]
        interaction_target=interaction_target,  # type: ignore[arg-type]
    )


def _make_commit(
    run_id: str,
    *,
    offset: int = 1,
    action: Action | None = None,
    event_types: list[str] | None = None,
    event_authority: str = "authoritative_fact",
) -> ActionCommit:
    if event_types is None:
        event_types = ["run.started"]
    events = [
        _make_event(run_id, offset + i, et, event_authority=event_authority)
        for i, et in enumerate(event_types)
    ]
    return ActionCommit(
        run_id=run_id,
        commit_id=f"c-{uuid.uuid4().hex[:8]}",
        events=events,
        created_at=_now(),
        action=action,
    )


def _make_exporter(
    provider: MockTracerProvider | None = None,
    include_payload: bool = False,
) -> Any:
    """Creates OTLPRunTraceExporter with the mock provider bypassing OTel import."""
    from agent_kernel.runtime.otel_export import OTLPRunTraceExporter

    provider = provider or MockTracerProvider()
    exporter = object.__new__(OTLPRunTraceExporter)
    exporter._tracer = provider.tracer  # type: ignore[attr-defined]
    exporter._include_payload = include_payload  # type: ignore[attr-defined]
    return exporter, provider.tracer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOTLPRunTraceExporter:

    def test_action_commit_produces_one_span(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-1")
        commit = _make_commit("run-1", action=action, event_types=["turn.dispatched"])
        asyncio.run(exporter.export_commit(commit))
        assert len(tracer.spans) == 1

    def test_span_name_is_kernel_turn_for_action_commit(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-1")
        commit = _make_commit("run-1", action=action)
        asyncio.run(exporter.export_commit(commit))
        assert tracer.spans[0].name == "kernel.turn"

    def test_span_name_is_kernel_lifecycle_for_no_action_commit(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit("run-2", event_types=["run.started"])
        asyncio.run(exporter.export_commit(commit))
        assert tracer.spans[0].name == "kernel.lifecycle"

    def test_span_attributes_include_run_id_and_commit_id(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit("run-3")
        asyncio.run(exporter.export_commit(commit))
        attrs = tracer.spans[0].attributes
        assert attrs["kernel.run_id"] == "run-3"
        assert attrs["kernel.commit_id"] == commit.commit_id

    def test_span_attributes_include_action_fields(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-4", effect_class="compensatable_write", action_type="file_write")
        commit = _make_commit("run-4", action=action)
        asyncio.run(exporter.export_commit(commit))
        attrs = tracer.spans[0].attributes
        assert attrs["kernel.action_id"] == action.action_id
        assert attrs["kernel.action_type"] == "file_write"
        assert attrs["kernel.effect_class"] == "compensatable_write"

    def test_interaction_target_included_in_span_attributes(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-5", interaction_target="it_service")
        commit = _make_commit("run-5", action=action)
        asyncio.run(exporter.export_commit(commit))
        assert tracer.spans[0].attributes["kernel.interaction_target"] == "it_service"

    def test_interaction_target_absent_when_none(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-6", interaction_target=None)
        commit = _make_commit("run-6", action=action)
        asyncio.run(exporter.export_commit(commit))
        assert "kernel.interaction_target" not in tracer.spans[0].attributes

    def test_span_events_one_per_runtime_event(self) -> None:
        exporter, tracer = _make_exporter()
        action = _make_action("run-7")
        commit = _make_commit(
            "run-7",
            action=action,
            event_types=["turn.dispatched", "turn.dispatch_acknowledged"],
        )
        asyncio.run(exporter.export_commit(commit))
        span = tracer.spans[0]
        assert len(span.events) == 2
        assert span.events[0].name == "turn.dispatched"
        assert span.events[1].name == "turn.dispatch_acknowledged"

    def test_span_event_attributes_include_event_authority(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit(
            "run-8",
            event_types=["run.ready"],
            event_authority="derived_diagnostic",
        )
        asyncio.run(exporter.export_commit(commit))
        event_attrs = tracer.spans[0].events[0].attributes
        assert event_attrs["event_authority"] == "derived_diagnostic"

    def test_span_event_attributes_include_commit_offset(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit("run-9", offset=7, event_types=["run.ready"])
        asyncio.run(exporter.export_commit(commit))
        assert tracer.spans[0].events[0].attributes["commit_offset"] == 7

    def test_span_event_count_matches_runtime_event_count(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit(
            "run-10",
            event_types=["run.created", "run.started", "run.ready"],
        )
        asyncio.run(exporter.export_commit(commit))
        assert len(tracer.spans[0].events) == 3
        assert tracer.spans[0].attributes["kernel.event_count"] == 3

    def test_payload_not_included_by_default(self) -> None:
        exporter, tracer = _make_exporter(include_payload=False)
        event = _make_event("run-11", 1, "turn.dispatched", payload_json={"host": "local"})
        commit = ActionCommit(
            run_id="run-11",
            commit_id="c-11",
            events=[event],
            created_at=_now(),
        )
        asyncio.run(exporter.export_commit(commit))
        event_attrs = tracer.spans[0].events[0].attributes
        assert "payload.host" not in event_attrs

    def test_payload_included_when_flag_set(self) -> None:
        exporter, tracer = _make_exporter(include_payload=True)
        event = _make_event("run-12", 1, "turn.dispatched", payload_json={"host": "local"})
        commit = ActionCommit(
            run_id="run-12",
            commit_id="c-12",
            events=[event],
            created_at=_now(),
        )
        asyncio.run(exporter.export_commit(commit))
        event_attrs = tracer.spans[0].events[0].attributes
        assert event_attrs["payload.host"] == "local"

    def test_multiple_commits_produce_independent_spans(self) -> None:
        exporter, tracer = _make_exporter()
        for i in range(3):
            asyncio.run(exporter.export_commit(_make_commit(f"run-{i}")))
        assert len(tracer.spans) == 3
        run_ids = {s.attributes["kernel.run_id"] for s in tracer.spans}
        assert run_ids == {"run-0", "run-1", "run-2"}

    def test_span_start_time_set_from_commit_timestamp(self) -> None:
        exporter, tracer = _make_exporter()
        commit = _make_commit("run-ts")
        asyncio.run(exporter.export_commit(commit))
        # start_time is in nanoseconds (large positive int) or None
        assert tracer.spans[0].start_time is not None
        assert tracer.spans[0].start_time > 0

    def test_caused_by_included_in_attributes(self) -> None:
        exporter, tracer = _make_exporter()
        commit = ActionCommit(
            run_id="run-cb",
            commit_id="c-cb",
            events=[_make_event("run-cb", 1, "run.started")],
            created_at=_now(),
            caused_by="parent-commit-id",
        )
        asyncio.run(exporter.export_commit(commit))
        assert tracer.spans[0].attributes["kernel.caused_by"] == "parent-commit-id"

    def test_instantiation_raises_import_error_without_otel(self) -> None:
        """OTLPRunTraceExporter raises ImportError when opentelemetry-api absent."""
        import sys

        # Temporarily hide opentelemetry from sys.modules
        saved = {k: v for k, v in sys.modules.items() if "opentelemetry" in k}
        try:
            for k in list(sys.modules):
                if "opentelemetry" in k:
                    del sys.modules[k]
            # Also block the import itself
            import builtins
            original_import = builtins.__import__

            def _blocking_import(name: str, *args: Any, **kwargs: Any) -> Any:
                if name.startswith("opentelemetry"):
                    raise ImportError(f"mocked missing: {name}")
                return original_import(name, *args, **kwargs)

            builtins.__import__ = _blocking_import
            try:
                from agent_kernel.runtime.otel_export import OTLPRunTraceExporter

                with pytest.raises(ImportError, match="opentelemetry-api"):
                    OTLPRunTraceExporter()
            finally:
                builtins.__import__ = original_import
        finally:
            sys.modules.update(saved)
