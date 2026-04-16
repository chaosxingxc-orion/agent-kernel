"""Verifies for deterministic idempotency key generation policy."""

from __future__ import annotations

from agent_kernel.kernel.contracts import Action, EffectClass
from agent_kernel.kernel.idempotency_key_policy import IdempotencyKeyPolicy


def _action(
    *,
    action_id: str = "a-1",
    payload: dict | None = None,
) -> Action:
    """Builds an action fixture."""
    return Action(
        action_id=action_id,
        run_id="run-1",
        action_type="tool_call",
        effect_class=EffectClass.IDEMPOTENT_WRITE,
        input_json=payload or {"x": 1},
        policy_tags=["tag-a"],
    )


def test_generate_is_deterministic_for_same_input() -> None:
    """Verifies generate is deterministic for same input."""
    action = _action()
    key_1 = IdempotencyKeyPolicy.generate("run-1", action, "snapshot-hash")
    key_2 = IdempotencyKeyPolicy.generate("run-1", action, "snapshot-hash")
    assert key_1 == key_2
    assert key_1.startswith("dispatch:run-1:a-1:")


def test_generate_changes_when_action_payload_changes() -> None:
    """Verifies generate changes when action payload changes."""
    key_1 = IdempotencyKeyPolicy.generate("run-1", _action(payload={"x": 1}), "snapshot-hash")
    key_2 = IdempotencyKeyPolicy.generate("run-1", _action(payload={"x": 2}), "snapshot-hash")
    assert key_1 != key_2


def test_generate_changes_when_snapshot_hash_changes() -> None:
    """Verifies generate changes when snapshot hash changes."""
    action = _action()
    key_1 = IdempotencyKeyPolicy.generate("run-1", action, "snap-a")
    key_2 = IdempotencyKeyPolicy.generate("run-1", action, "snap-b")
    assert key_1 != key_2


def test_generate_compensation_key_uses_standard_format() -> None:
    """Verifies generate compensation key uses standard format."""
    key = IdempotencyKeyPolicy.generate_compensation_key("write", "act-7")
    assert key == "compensation:write:act-7"
