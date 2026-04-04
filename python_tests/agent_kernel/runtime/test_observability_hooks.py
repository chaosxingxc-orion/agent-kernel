"""Tests for LoggingObservabilityHook JSON mode (R3a) and schema_version (R3b)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from agent_kernel.runtime.observability_hooks import LoggingObservabilityHook

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Logging handler that stores formatted messages for test inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.messages.append(self.format(record))


def _attach_handler(logger_name: str) -> _CapturingHandler:
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


# ---------------------------------------------------------------------------
# R3a — JSON structured logging
# ---------------------------------------------------------------------------


class TestLoggingObservabilityHookKeyValue:
    """Verifies the default key=value text format is preserved."""

    def test_turn_transition_text_contains_run_id(self) -> None:
        logger_name = "test_kv_turn"
        handler = _attach_handler(logger_name)
        hook = LoggingObservabilityHook(logger_name=logger_name, use_json=False)
        hook.on_turn_state_transition(
            run_id="run-1",
            action_id="act-1",
            from_state="collecting",
            to_state="intent_committed",
            turn_offset=1,
            timestamp_ms=1000,
        )
        assert handler.messages, "Expected at least one log message"
        msg = handler.messages[0]
        assert "run-1" in msg
        assert "turn_transition" in msg

    def test_recovery_text_contains_mode(self) -> None:
        logger_name = "test_kv_recovery"
        handler = _attach_handler(logger_name)
        hook = LoggingObservabilityHook(logger_name=logger_name, use_json=False)
        hook.on_recovery_triggered(run_id="run-2", reason_code="runtime_error", mode="abort")
        msg = handler.messages[0]
        assert "abort" in msg
        assert "runtime_error" in msg


class TestLoggingObservabilityHookJSON:
    """Verifies JSON mode emits valid JSON with expected fields."""

    def _hook_and_handler(self) -> tuple[LoggingObservabilityHook, _CapturingHandler]:
        import uuid

        name = f"test_json_{uuid.uuid4().hex[:8]}"
        handler = _attach_handler(name)
        hook = LoggingObservabilityHook(logger_name=name, use_json=True)
        return hook, handler

    def _parse(self, handler: _CapturingHandler) -> dict[str, Any]:
        assert handler.messages, "No log message emitted"
        return json.loads(handler.messages[0])

    def test_turn_transition_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_turn_state_transition(
            run_id="run-1",
            action_id="act-1",
            from_state="collecting",
            to_state="intent_committed",
            turn_offset=1,
            timestamp_ms=2000,
        )
        obj = self._parse(handler)
        assert obj["event"] == "turn_transition"
        assert obj["run_id"] == "run-1"
        assert obj["action_id"] == "act-1"
        assert obj["from_state"] == "collecting"
        assert obj["to_state"] == "intent_committed"
        assert "ts_ms" in obj
        assert isinstance(obj["ts_ms"], int)

    def test_run_lifecycle_transition_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_run_lifecycle_transition(
            run_id="run-2",
            from_state="ready",
            to_state="dispatching",
            timestamp_ms=3000,
        )
        obj = self._parse(handler)
        assert obj["event"] == "run_transition"
        assert obj["run_id"] == "run-2"
        assert obj["from_state"] == "ready"

    def test_llm_call_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_llm_call(run_id="run-3", model_ref="gpt-4o", latency_ms=120, token_usage=None)
        obj = self._parse(handler)
        assert obj["event"] == "llm_call"
        assert obj["model_ref"] == "gpt-4o"
        assert obj["tok_in"] == 0
        assert obj["tok_out"] == 0

    def test_recovery_triggered_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_recovery_triggered(run_id="run-4", reason_code="timeout", mode="abort")
        obj = self._parse(handler)
        assert obj["event"] == "recovery_triggered"
        assert obj["mode"] == "abort"

    def test_admission_evaluated_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_admission_evaluated(run_id="run-5", action_id="act-5", admitted=True, latency_ms=5)
        obj = self._parse(handler)
        assert obj["event"] == "admission_evaluated"
        assert obj["admitted"] is True

    def test_parallel_branch_result_json_fields(self) -> None:
        hook, handler = self._hook_and_handler()
        hook.on_parallel_branch_result(
            run_id="run-6",
            group_idempotency_key="grp-1",
            action_id="act-6",
            outcome="failed",
            failure_code="TimeoutError",
        )
        obj = self._parse(handler)
        assert obj["event"] == "branch_result"
        assert obj["outcome"] == "failed"
        assert obj["failure_code"] == "TimeoutError"

    def test_json_record_is_valid_json(self) -> None:
        """Every emitted line must be parseable as JSON."""
        hook, handler = self._hook_and_handler()
        hook.on_action_dispatch(
            run_id="run-7",
            action_id="act-7",
            action_type="tool_call",
            outcome_kind="dispatched",
            latency_ms=10,
        )
        # Should not raise
        parsed = json.loads(handler.messages[0])
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# R3b — RuntimeEvent schema_version field
# ---------------------------------------------------------------------------


class TestRuntimeEventSchemaVersion:
    def test_runtime_event_has_schema_version_field(self) -> None:
        from agent_kernel.kernel.contracts import RuntimeEvent

        event = RuntimeEvent(
            run_id="run-1",
            event_id="evt-1",
            commit_offset=1,
            event_type="run.started",
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key="run-1:1",
            wake_policy="wake_actor",
            created_at="2026-04-03T00:00:00Z",
        )
        assert event.schema_version == "1"

    def test_runtime_event_schema_version_default_is_current(self) -> None:
        from agent_kernel.kernel.contracts import RuntimeEvent
        from agent_kernel.kernel.event_registry import _CURRENT_EVENT_SCHEMA_VERSION

        event = RuntimeEvent(
            run_id="r",
            event_id="e",
            commit_offset=0,
            event_type="run.started",
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key="k",
            wake_policy="wake_actor",
            created_at="2026-04-03T00:00:00Z",
        )
        assert event.schema_version == _CURRENT_EVENT_SCHEMA_VERSION

    def test_validate_event_schema_version_current_returns_true(self) -> None:
        from agent_kernel.kernel.event_registry import validate_event_schema_version

        assert validate_event_schema_version("1") is True

    def test_validate_event_schema_version_unknown_returns_false(self) -> None:
        from agent_kernel.kernel.event_registry import validate_event_schema_version

        assert validate_event_schema_version("99") is False

    def test_validate_event_schema_version_strict_raises_on_mismatch(self) -> None:
        from agent_kernel.kernel.event_registry import validate_event_schema_version

        with pytest.raises(ValueError, match="schema_version"):
            validate_event_schema_version("0", strict=True)

    def test_event_type_descriptor_carries_schema_version(self) -> None:
        from agent_kernel.kernel.event_registry import (
            _CURRENT_EVENT_SCHEMA_VERSION,
            KERNEL_EVENT_REGISTRY,
        )

        descriptor = KERNEL_EVENT_REGISTRY.get("run.started")
        assert descriptor is not None
        assert descriptor.schema_version == _CURRENT_EVENT_SCHEMA_VERSION
