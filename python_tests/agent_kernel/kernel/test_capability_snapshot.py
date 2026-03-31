"""Tests for v6.4 capability snapshot canonicalization and stable hashing."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
    CapabilitySnapshotBuildError,
    CapabilitySnapshotInput,
    DeclarativeBundleDigest,
)


def _build_input() -> CapabilitySnapshotInput:
    """Builds a baseline snapshot input used across canonicalization tests."""
    return CapabilitySnapshotInput(
        run_id="run-1",
        based_on_offset=12,
        tenant_policy_ref="policy:v1",
        permission_mode="strict",
        tool_bindings=["tool.beta", "tool.alpha", "tool.alpha"],
        mcp_bindings=["mcp.docs.fetch", "mcp.docs.fetch", "mcp.search.query"],
        skill_bindings=["skill.plan", "skill.execute", "skill.plan"],
        feature_flags=["ff.debug", "ff.fast_path", "ff.debug"],
        context_binding_ref="ctx://workspace/rules.md",
        context_content_hash="ctx-hash-1",
        budget_ref="budget:team-a",
        quota_ref="quota:team-a",
        session_mode="interactive",
        approval_state="approved",
        declarative_bundle_digest=DeclarativeBundleDigest(
            bundle_ref="bundle:v1",
            semantics_version="v1",
            content_hash="content-hash-1",
            compile_hash="compile-hash-1",
        ),
    )


def test_same_semantic_input_with_different_order_has_same_snapshot_hash() -> None:
    """Builder should produce same hash for semantically equivalent unordered inputs."""
    builder = CapabilitySnapshotBuilder()
    input_a = _build_input()
    input_b = CapabilitySnapshotInput(
        run_id=input_a.run_id,
        based_on_offset=input_a.based_on_offset,
        tenant_policy_ref=input_a.tenant_policy_ref,
        permission_mode=input_a.permission_mode,
        tool_bindings=list(reversed(input_a.tool_bindings)),
        mcp_bindings=list(reversed(input_a.mcp_bindings)),
        skill_bindings=list(reversed(input_a.skill_bindings)),
        feature_flags=list(reversed(input_a.feature_flags)),
        context_binding_ref=input_a.context_binding_ref,
        context_content_hash=input_a.context_content_hash,
        budget_ref=input_a.budget_ref,
        quota_ref=input_a.quota_ref,
        session_mode=input_a.session_mode,
        approval_state=input_a.approval_state,
        declarative_bundle_digest=input_a.declarative_bundle_digest,
    )

    snapshot_a = builder.build(input_a)
    snapshot_b = builder.build(input_b)

    assert snapshot_a.snapshot_hash == snapshot_b.snapshot_hash
    assert snapshot_a.tool_bindings == ["tool.alpha", "tool.beta"]
    assert snapshot_a.feature_flags == ["ff.debug", "ff.fast_path"]


def test_same_context_content_with_different_context_ref_has_same_snapshot_hash() -> None:
    """Builder should hash context by content hash, not by mutable context reference."""
    builder = CapabilitySnapshotBuilder()
    input_a = _build_input()
    input_b = CapabilitySnapshotInput(
        run_id=input_a.run_id,
        based_on_offset=input_a.based_on_offset,
        tenant_policy_ref=input_a.tenant_policy_ref,
        permission_mode=input_a.permission_mode,
        tool_bindings=input_a.tool_bindings,
        mcp_bindings=input_a.mcp_bindings,
        skill_bindings=input_a.skill_bindings,
        feature_flags=input_a.feature_flags,
        context_binding_ref="ctx://workspace/renamed-rules.md",
        context_content_hash=input_a.context_content_hash,
        budget_ref=input_a.budget_ref,
        quota_ref=input_a.quota_ref,
        session_mode=input_a.session_mode,
        approval_state=input_a.approval_state,
        declarative_bundle_digest=input_a.declarative_bundle_digest,
    )

    snapshot_a = builder.build(input_a)
    snapshot_b = builder.build(input_b)

    assert snapshot_a.snapshot_hash == snapshot_b.snapshot_hash


def test_missing_required_context_content_hash_raises_build_error() -> None:
    """Builder must fail fast when context ref exists but context content hash is missing."""
    builder = CapabilitySnapshotBuilder()
    input_value = _build_input()
    input_value = CapabilitySnapshotInput(
        run_id=input_value.run_id,
        based_on_offset=input_value.based_on_offset,
        tenant_policy_ref=input_value.tenant_policy_ref,
        permission_mode=input_value.permission_mode,
        tool_bindings=input_value.tool_bindings,
        mcp_bindings=input_value.mcp_bindings,
        skill_bindings=input_value.skill_bindings,
        feature_flags=input_value.feature_flags,
        context_binding_ref=input_value.context_binding_ref,
        context_content_hash=None,
        budget_ref=input_value.budget_ref,
        quota_ref=input_value.quota_ref,
        session_mode=input_value.session_mode,
        approval_state=input_value.approval_state,
        declarative_bundle_digest=input_value.declarative_bundle_digest,
    )

    with pytest.raises(CapabilitySnapshotBuildError):
        builder.build(input_value)


def test_different_based_on_offset_produces_different_snapshot_hash() -> None:
    """Builder should include based_on_offset in canonical hash input."""
    builder = CapabilitySnapshotBuilder()
    input_a = _build_input()
    input_b = CapabilitySnapshotInput(
        run_id=input_a.run_id,
        based_on_offset=input_a.based_on_offset + 1,
        tenant_policy_ref=input_a.tenant_policy_ref,
        permission_mode=input_a.permission_mode,
        tool_bindings=input_a.tool_bindings,
        mcp_bindings=input_a.mcp_bindings,
        skill_bindings=input_a.skill_bindings,
        feature_flags=input_a.feature_flags,
        context_binding_ref=input_a.context_binding_ref,
        context_content_hash=input_a.context_content_hash,
        budget_ref=input_a.budget_ref,
        quota_ref=input_a.quota_ref,
        session_mode=input_a.session_mode,
        approval_state=input_a.approval_state,
        declarative_bundle_digest=input_a.declarative_bundle_digest,
    )

    snapshot_a = builder.build(input_a)
    snapshot_b = builder.build(input_b)

    assert snapshot_a.snapshot_hash != snapshot_b.snapshot_hash
