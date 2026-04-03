"""Output parser implementations for the cognitive layer.

Provides:
  - ``ToolCallOutputParser`` — parses ``tool_calls`` from ``ModelOutput``
    into kernel ``Action`` objects.
  - ``JSONModeOutputParser`` — parses a JSON array from ``ModelOutput.raw_text``
    into kernel ``Action`` objects.

Both parsers implement the ``OutputParser`` protocol defined in ``contracts.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from agent_kernel.kernel.contracts import (
    Action,
    EffectClass,
    ExecutionPlan,
    InteractionTarget,
    ModelOutput,
    SequentialPlan,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default effect class for unknown tool names
# ---------------------------------------------------------------------------

_DEFAULT_EFFECT_CLASS: EffectClass = "read_only"

# ---------------------------------------------------------------------------
# ToolCallOutputParser
# ---------------------------------------------------------------------------


class ToolCallOutputParser:
    """Parses tool_calls from ``ModelOutput`` into kernel ``Action`` objects.

    Each tool call in ``ModelOutput.tool_calls`` is translated into one
    ``Action``.  The effect class for each action is resolved via
    ``tool_effect_class_map``; unknown tool names fall back to
    ``"read_only"``.

    Attributes:
        _tool_effect_class_map: Mapping from tool name to ``EffectClass``.
    """

    def __init__(
        self,
        tool_effect_class_map: dict[str, EffectClass] | None = None,
    ) -> None:
        """Initialises the parser with an optional tool effect class map.

        Args:
            tool_effect_class_map: Optional mapping from tool name to
                ``EffectClass``.  Defaults to empty (all tools map to
                ``"read_only"``).
        """
        self._tool_effect_class_map: dict[str, EffectClass] = (
            tool_effect_class_map if tool_effect_class_map is not None else {}
        )

    def parse(self, output: ModelOutput, run_id: str) -> list[Action]:
        """Parses tool_calls from ``ModelOutput`` into a flat list of ``Action`` objects.

        Args:
            output: Normalised model output containing tool_calls.
            run_id: Run identifier used to populate each ``Action.run_id``.

        Returns:
            Ordered list of kernel ``Action`` objects.  Empty when
            ``output.tool_calls`` is empty.
        """
        actions: list[Action] = []
        for tool_call in output.tool_calls:
            action_id = self._resolve_action_id(tool_call)
            tool_name = tool_call.get("name", "")
            effect_class: EffectClass = self._tool_effect_class_map.get(
                tool_name, _DEFAULT_EFFECT_CLASS
            )
            input_json: dict[str, Any] | None = tool_call.get("arguments")
            if not isinstance(input_json, dict):
                input_json = None

            actions.append(
                Action(
                    action_id=action_id,
                    run_id=run_id,
                    action_type=tool_name,
                    effect_class=effect_class,
                    input_json=input_json,
                )
            )
        return actions

    def parse_plan(self, output: ModelOutput, run_id: str) -> ExecutionPlan:
        """Parses tool_calls from ``ModelOutput`` into a ``SequentialPlan``.

        Args:
            output: Normalised model output.
            run_id: Run identifier.

        Returns:
            ``SequentialPlan`` wrapping the parsed actions as sequential steps.
        """
        return SequentialPlan(steps=tuple(self.parse(output, run_id)))

    @staticmethod
    def _resolve_action_id(tool_call: dict[str, Any]) -> str:
        """Resolves a stable action identifier from a tool call dict.

        Uses the ``id`` field when present and non-empty; otherwise generates
        a fresh UUID.

        Args:
            tool_call: Provider-neutral tool call dict.

        Returns:
            Non-empty action identifier string.
        """
        raw_id = tool_call.get("id", "")
        if raw_id:
            return f"act-{raw_id}"
        return f"act-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# JSONModeOutputParser
# ---------------------------------------------------------------------------

_VALID_EFFECT_CLASSES: frozenset[str] = frozenset(
    ["read_only", "idempotent_write", "compensatable_write", "irreversible_write"]
)

_VALID_INTERACTION_TARGETS: frozenset[str] = frozenset(
    [
        "agent_peer",
        "it_service",
        "data_system",
        "tool_executor",
        "human_actor",
        "event_stream",
    ]
)


class JSONModeOutputParser:
    """Parses structured JSON from ``ModelOutput.raw_text`` into ``Action`` objects.

    Expects ``ModelOutput.raw_text`` to be a JSON array where each element
    is an action object with at minimum ``action_type`` and ``effect_class``
    fields.

    Falls back to an empty list on parse errors and logs a warning rather
    than raising, to keep the TurnEngine FSM recoverable.
    """

    def parse(self, output: ModelOutput, run_id: str) -> list[Action]:
        """Parses a JSON array from ``ModelOutput.raw_text`` into ``Action`` objects.

        Args:
            output: Normalised model output whose ``raw_text`` contains a
                JSON array of action objects.
            run_id: Run identifier used to populate each ``Action.run_id``.

        Returns:
            Ordered list of kernel ``Action`` objects.  Returns an empty list
            when ``raw_text`` cannot be parsed or does not contain a valid
            JSON array.
        """
        raw = (output.raw_text or "").strip()
        if not raw:
            return []

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _LOG.warning(
                "JSONModeOutputParser: failed to parse raw_text as JSON for run_id=%s",
                run_id,
            )
            return []

        if not isinstance(parsed, list):
            _LOG.warning(
                "JSONModeOutputParser: expected a JSON array for run_id=%s, got %s",
                run_id,
                type(parsed).__name__,
            )
            return []

        actions: list[Action] = []
        for idx, item in enumerate(parsed):
            action = self._parse_item(item, run_id, idx)
            if action is not None:
                actions.append(action)
        return actions

    def parse_plan(self, output: ModelOutput, run_id: str) -> ExecutionPlan:
        """Parses JSON array from ``ModelOutput.raw_text`` into a ``SequentialPlan``.

        Args:
            output: Normalised model output.
            run_id: Run identifier.

        Returns:
            ``SequentialPlan`` wrapping the parsed actions as sequential steps.
        """
        return SequentialPlan(steps=tuple(self.parse(output, run_id)))

    @staticmethod
    def _parse_item(
        item: Any,
        run_id: str,
        idx: int,
    ) -> Action | None:
        """Parses one action object from a JSON array element.

        Args:
            item: JSON object candidate (expected to be a dict).
            run_id: Run identifier.
            idx: Index within the array (for error messages).

        Returns:
            Parsed ``Action``, or ``None`` when the item is invalid.
        """
        if not isinstance(item, dict):
            _LOG.warning(
                "JSONModeOutputParser: item[%d] is not a dict for run_id=%s", idx, run_id
            )
            return None

        action_type = item.get("action_type")
        effect_class_raw = item.get("effect_class")

        if not action_type or not isinstance(action_type, str):
            _LOG.warning(
                "JSONModeOutputParser: item[%d] missing action_type for run_id=%s", idx, run_id
            )
            return None

        if effect_class_raw not in _VALID_EFFECT_CLASSES:
            _LOG.warning(
                "JSONModeOutputParser: item[%d] has invalid effect_class=%r for run_id=%s",
                idx,
                effect_class_raw,
                run_id,
            )
            return None

        effect_class: EffectClass = effect_class_raw  # type: ignore[assignment]

        input_json: dict[str, Any] | None = item.get("input_json")
        if input_json is not None and not isinstance(input_json, dict):
            input_json = None

        interaction_target_raw = item.get("interaction_target")
        interaction_target: InteractionTarget | None = None
        if interaction_target_raw in _VALID_INTERACTION_TARGETS:
            interaction_target = interaction_target_raw  # type: ignore[assignment]

        timeout_ms = item.get("timeout_ms")
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            timeout_ms = None

        action_id = item.get("action_id") or f"act-{uuid.uuid4().hex}"

        return Action(
            action_id=action_id,
            run_id=run_id,
            action_type=action_type,
            effect_class=effect_class,
            input_json=input_json,
            interaction_target=interaction_target,
            timeout_ms=timeout_ms,
        )
