"""Verifies for action-payload capability snapshot input resolver."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuildError
from agent_kernel.kernel.capability_snapshot_resolver import (
    ActionPayloadCapabilitySnapshotInputResolver,
)
from agent_kernel.kernel.contracts import Action, EffectClass
from agent_kernel.kernel.turn_engine import TurnInput


def _build_turn_input() -> TurnInput:
    """Builds deterministic turn input for resolver tests."""
    return TurnInput(
        run_id="run-1",
        through_offset=10,
        based_on_offset=10,
        trigger_type="start",
    )


def test_resolver_uses_structured_payload_and_applies_source_priority() -> None:
    """Structured nested sources should override flat fallback values."""
    resolver = ActionPayloadCapabilitySnapshotInputResolver()
    action = Action(
        action_id="action-1",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.READ_ONLY,
        input_json={
            "capability_snapshot_input": {
                "tenant_policy_ref": "flat-policy",
                "permission_mode": "flat-permission",
                "policy": {
                    "tenant_policy_ref": "nested-policy",
                    "permission_mode": "nested-permission",
                },
                "capability_bindings": {
                    "tool_bindings": ["tool.alpha"],
                    "mcp_bindings": ["mcp.docs.fetch"],
                    "skill_bindings": ["skill.plan"],
                },
                "approval": {"approval_state": "approved"},
                "budget": {"budget_ref": "budget-a", "quota_ref": "quota-a"},
                "session": {"session_mode": "interactive"},
                "feature_flags": ["ff.a", "ff.b"],
                "declarative_bundle_digest": {
                    "bundle_ref": "bundle:v1",
                    "semantics_version": "v1",
                    "content_hash": "content-hash-1",
                    "compile_hash": "compile-hash-1",
                },
            }
        },
    )

    resolved = resolver.resolve(_build_turn_input(), action)

    assert resolved.tenant_policy_ref == "nested-policy"
    assert resolved.permission_mode == "nested-permission"
    assert resolved.tool_bindings == ["tool.alpha"]
    assert resolved.approval_state == "approved"
    assert resolved.budget_ref == "budget-a"
    assert resolved.declarative_bundle_digest is not None
    assert resolved.declarative_bundle_digest.bundle_ref == "bundle:v1"


def test_resolver_falls_back_to_defaults_when_payload_missing() -> None:
    """Missing payload should produce deterministic default snapshot inputs."""
    resolver = ActionPayloadCapabilitySnapshotInputResolver()
    action = Action(
        action_id="action-2",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.READ_ONLY,
        input_json={"query": "hello"},
    )

    resolved = resolver.resolve(_build_turn_input(), action)

    assert resolved.tenant_policy_ref == "policy:default"
    assert resolved.permission_mode == "strict"
    assert not resolved.tool_bindings


def test_resolver_strict_mode_requires_declared_snapshot_payload() -> None:
    """Strict mode should fail when declared snapshot payload is absent."""
    resolver = ActionPayloadCapabilitySnapshotInputResolver(require_declared_snapshot_input=True)
    action = Action(
        action_id="action-strict-missing-payload",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.READ_ONLY,
        input_json={"query": "hello"},
    )

    with pytest.raises(
        CapabilitySnapshotBuildError,
        match=r"capability_snapshot_input is required in strict mode\.",
    ):
        resolver.resolve(_build_turn_input(), action)


def test_resolver_strict_mode_requires_declarative_bundle_digest() -> None:
    """Strict mode should fail when declared bundle digest is absent."""
    resolver = ActionPayloadCapabilitySnapshotInputResolver(require_declarative_bundle_digest=True)
    action = Action(
        action_id="action-strict-missing-digest",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.READ_ONLY,
        input_json={
            "capability_snapshot_input": {
                "tenant_policy_ref": "policy:v1",
                "permission_mode": "strict",
            }
        },
    )

    with pytest.raises(
        CapabilitySnapshotBuildError,
        match=r"declarative_bundle_digest is required in strict mode\.",
    ):
        resolver.resolve(_build_turn_input(), action)


def test_resolver_raises_when_declarative_bundle_digest_is_partial() -> None:
    """Resolver should reject partial declarative digest payloads."""
    resolver = ActionPayloadCapabilitySnapshotInputResolver()
    action = Action(
        action_id="action-partial-digest",
        run_id="run-1",
        action_type="tool.search",
        effect_class=EffectClass.READ_ONLY,
        input_json={
            "capability_snapshot_input": {
                "tenant_policy_ref": "policy:v1",
                "permission_mode": "strict",
                "declarative_bundle_digest": {
                    "bundle_ref": "bundle:v1",
                },
            }
        },
    )

    with pytest.raises(
        CapabilitySnapshotBuildError,
        match=r"declarative_bundle_digest is missing required fields:",
    ):
        resolver.resolve(_build_turn_input(), action)
