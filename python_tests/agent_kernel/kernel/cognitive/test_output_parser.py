"""Tests for ToolCallOutputParser and JSONModeOutputParser."""

from __future__ import annotations

import json

from agent_kernel.kernel.cognitive.output_parser import (
    JSONModeOutputParser,
    ToolCallOutputParser,
)
from agent_kernel.kernel.contracts import (
    Action,
    ModelOutput,
    SequentialPlan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_output(
    raw_text: str = "",
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
) -> ModelOutput:
    """Builds a minimal ModelOutput for tests."""
    return ModelOutput(
        raw_text=raw_text,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# ToolCallOutputParser tests
# ---------------------------------------------------------------------------


class TestToolCallOutputParser:
    """Tests for ToolCallOutputParser."""

    def test_parse_empty_tool_calls_returns_empty_list(self) -> None:
        """parse() should return [] when ModelOutput has no tool_calls."""
        parser = ToolCallOutputParser()
        output = _make_output(tool_calls=[])

        result = parser.parse(output, "run-1")

        assert result == []

    def test_parse_single_tool_call_returns_one_action(self) -> None:
        """parse() should return one Action for a single tool_call."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "search", "arguments": {"q": "hello"}}],
            finish_reason="tool_calls",
        )

        result = parser.parse(output, "run-42")

        assert len(result) == 1
        action = result[0]
        assert isinstance(action, Action)

    def test_parse_action_run_id_populated(self) -> None:
        """parse() should set action.run_id from the run_id parameter."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "fetch", "arguments": {}}]
        )

        result = parser.parse(output, "run-99")

        assert result[0].run_id == "run-99"

    def test_parse_action_type_from_tool_name(self) -> None:
        """action_type should equal the tool_call name."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "write_file", "arguments": {}}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].action_type == "write_file"

    def test_parse_action_id_prefixed_with_act(self) -> None:
        """action_id should be prefixed with 'act-' for named tool calls."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-xyz", "name": "tool", "arguments": {}}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].action_id == "act-tc-xyz"

    def test_parse_action_id_generated_when_no_id(self) -> None:
        """action_id should be auto-generated when tool_call has no id."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"name": "tool", "arguments": {}}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].action_id.startswith("act-")

    def test_parse_default_effect_class_is_read_only(self) -> None:
        """effect_class should default to 'read_only' for unknown tools."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "unknown_tool", "arguments": {}}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].effect_class == "read_only"

    def test_parse_effect_class_from_map(self) -> None:
        """effect_class should be resolved from tool_effect_class_map."""
        parser = ToolCallOutputParser(
            tool_effect_class_map={"write_file": "compensatable_write"}
        )
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "write_file", "arguments": {"path": "/tmp/x"}}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].effect_class == "compensatable_write"

    def test_parse_input_json_from_arguments(self) -> None:
        """input_json should be populated from tool_call arguments."""
        parser = ToolCallOutputParser()
        args = {"query": "hello", "limit": 10}
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "search", "arguments": args}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].input_json == args

    def test_parse_input_json_none_when_arguments_not_dict(self) -> None:
        """input_json should be None when arguments is not a dict."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "tool", "arguments": "bad"}]
        )

        result = parser.parse(output, "run-1")

        assert result[0].input_json is None

    def test_parse_multiple_tool_calls_returns_multiple_actions(self) -> None:
        """parse() should return one Action per tool_call."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[
                {"id": "tc-1", "name": "search", "arguments": {}},
                {"id": "tc-2", "name": "write_file", "arguments": {}},
            ]
        )

        result = parser.parse(output, "run-1")

        assert len(result) == 2
        assert result[0].action_type == "search"
        assert result[1].action_type == "write_file"

    def test_parse_plan_returns_sequential_plan(self) -> None:
        """parse_plan() should return a SequentialPlan."""
        parser = ToolCallOutputParser()
        output = _make_output(
            tool_calls=[{"id": "tc-1", "name": "search", "arguments": {}}]
        )

        result = parser.parse_plan(output, "run-1")

        assert isinstance(result, SequentialPlan)
        assert len(result.steps) == 1

    def test_parse_plan_empty_steps_when_no_tool_calls(self) -> None:
        """parse_plan() should return SequentialPlan with empty steps."""
        parser = ToolCallOutputParser()
        output = _make_output(tool_calls=[])

        result = parser.parse_plan(output, "run-1")

        assert isinstance(result, SequentialPlan)
        assert result.steps == ()


# ---------------------------------------------------------------------------
# JSONModeOutputParser tests
# ---------------------------------------------------------------------------


class TestJSONModeOutputParser:
    """Tests for JSONModeOutputParser."""

    def test_parse_valid_json_returns_actions(self) -> None:
        """parse() should return Actions from a valid JSON array."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "search", "effect_class": "read_only"}
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert len(result) == 1
        assert result[0].action_type == "search"
        assert result[0].effect_class == "read_only"

    def test_parse_invalid_json_returns_empty_list(self) -> None:
        """parse() should return [] when raw_text is not valid JSON."""
        parser = JSONModeOutputParser()
        output = _make_output(raw_text="this is not json {{{")

        result = parser.parse(output, "run-1")

        assert result == []

    def test_parse_empty_raw_text_returns_empty_list(self) -> None:
        """parse() should return [] when raw_text is empty."""
        parser = JSONModeOutputParser()
        output = _make_output(raw_text="")

        result = parser.parse(output, "run-1")

        assert result == []

    def test_parse_json_object_not_array_returns_empty_list(self) -> None:
        """parse() should return [] when raw_text is a JSON object, not array."""
        parser = JSONModeOutputParser()
        output = _make_output(raw_text=json.dumps({"action_type": "search"}))

        result = parser.parse(output, "run-1")

        assert result == []

    def test_parse_action_run_id_from_parameter(self) -> None:
        """parsed Actions should have run_id from the run_id parameter."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "fetch", "effect_class": "read_only"}
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-77")

        assert result[0].run_id == "run-77"

    def test_parse_action_id_auto_generated(self) -> None:
        """action_id should be auto-generated when not provided."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "fetch", "effect_class": "read_only"}
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].action_id.startswith("act-")

    def test_parse_action_id_used_when_provided(self) -> None:
        """action_id should be used from JSON when present."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_id": "my-action-1", "action_type": "fetch", "effect_class": "read_only"}
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].action_id == "my-action-1"

    def test_parse_input_json_populated(self) -> None:
        """input_json should be populated from the JSON payload."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {
                "action_type": "write",
                "effect_class": "idempotent_write",
                "input_json": {"path": "/tmp/x", "content": "hello"},
            }
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].input_json == {"path": "/tmp/x", "content": "hello"}

    def test_parse_interaction_target_populated(self) -> None:
        """interaction_target should be populated from the JSON payload."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {
                "action_type": "call_api",
                "effect_class": "idempotent_write",
                "interaction_target": "it_service",
            }
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].interaction_target == "it_service"

    def test_parse_invalid_interaction_target_ignored(self) -> None:
        """interaction_target should be None when value is not valid."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {
                "action_type": "call_api",
                "effect_class": "read_only",
                "interaction_target": "bogus_target",
            }
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].interaction_target is None

    def test_parse_timeout_ms_populated(self) -> None:
        """timeout_ms should be populated from the JSON payload."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {
                "action_type": "fetch",
                "effect_class": "read_only",
                "timeout_ms": 5000,
            }
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].timeout_ms == 5000

    def test_parse_invalid_timeout_ms_ignored(self) -> None:
        """timeout_ms should be None when value is not a positive int."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {
                "action_type": "fetch",
                "effect_class": "read_only",
                "timeout_ms": -1,
            }
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert result[0].timeout_ms is None

    def test_parse_item_missing_action_type_skipped(self) -> None:
        """Items missing action_type should be silently skipped."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"effect_class": "read_only"},
            {"action_type": "valid_action", "effect_class": "read_only"},
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert len(result) == 1
        assert result[0].action_type == "valid_action"

    def test_parse_item_invalid_effect_class_skipped(self) -> None:
        """Items with invalid effect_class should be silently skipped."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "tool", "effect_class": "totally_invalid"},
            {"action_type": "valid_tool", "effect_class": "read_only"},
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert len(result) == 1
        assert result[0].action_type == "valid_tool"

    def test_parse_multiple_valid_items(self) -> None:
        """parse() should return one Action per valid JSON array element."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "search", "effect_class": "read_only"},
            {"action_type": "write", "effect_class": "compensatable_write"},
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert len(result) == 2

    def test_parse_plan_returns_sequential_plan(self) -> None:
        """parse_plan() should return a SequentialPlan."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            {"action_type": "search", "effect_class": "read_only"}
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse_plan(output, "run-1")

        assert isinstance(result, SequentialPlan)
        assert len(result.steps) == 1

    def test_parse_plan_empty_when_invalid_json(self) -> None:
        """parse_plan() should return empty SequentialPlan on invalid JSON."""
        parser = JSONModeOutputParser()
        output = _make_output(raw_text="not json")

        result = parser.parse_plan(output, "run-1")

        assert isinstance(result, SequentialPlan)
        assert result.steps == ()

    def test_parse_non_dict_array_item_skipped(self) -> None:
        """Non-dict items in the JSON array should be silently skipped."""
        parser = JSONModeOutputParser()
        payload = json.dumps([
            "a string item",
            {"action_type": "fetch", "effect_class": "read_only"},
        ])
        output = _make_output(raw_text=payload)

        result = parser.parse(output, "run-1")

        assert len(result) == 1
        assert result[0].action_type == "fetch"
