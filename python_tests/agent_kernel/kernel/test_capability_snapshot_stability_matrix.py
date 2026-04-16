"""Large stability matrix for CapabilitySnapshot canonicalization behavior.

The goal of this suite is to widen regression coverage space for future
evolution. Cases are deterministic (seed-based) and focus on invariants:
  1. Order/duplicate/noise differences must not change snapshot hash.
  2. Same semantic payload should produce same hash across repeat builds.
"""

from __future__ import annotations

import random

import pytest

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
    CapabilitySnapshotInput,
)

_CASE_COUNT = 1850


def _normalize(values: list[str]) -> list[str]:
    """Normalize."""
    return sorted({value for value in values if value})


def _make_noisy_values(
    rng: random.Random,
    pool: list[str],
    *,
    max_len: int = 12,
) -> list[str]:
    """Make noisy values."""
    size = rng.randint(0, max_len)
    values: list[str] = []
    for _ in range(size):
        pick = rng.choice([*pool, "", "", ""])
        values.append(pick)
    if pool and rng.random() < 0.7:
        values.append(rng.choice(pool))
    if pool and rng.random() < 0.4:
        values.append(rng.choice(pool))
    return values


@pytest.mark.parametrize("seed", list(range(_CASE_COUNT)))
def test_snapshot_hash_is_stable_for_semantically_equivalent_bindings(
    seed: int,
) -> None:  # pylint: disable=too-many-locals
    """Canonicalization must collapse ordering/duplication/noise variance."""
    rng = random.Random(seed)
    builder = CapabilitySnapshotBuilder()

    tool_pool = ["tool.search", "tool.write", "tool.plan", "tool.audit"]
    mcp_pool = ["mcp.alpha.read", "mcp.beta.write", "mcp.gamma.list"]
    skill_pool = ["skill.search.v1", "skill.review.v2", "skill.write.v1"]
    flag_pool = ["flag_a", "flag_b", "flag_c", "flag_d"]

    noisy_tools = _make_noisy_values(rng, tool_pool, max_len=10)
    noisy_mcp = _make_noisy_values(rng, mcp_pool, max_len=8)
    noisy_skills = _make_noisy_values(rng, skill_pool, max_len=8)
    noisy_flags = _make_noisy_values(rng, flag_pool, max_len=8)

    normalized_tools = _normalize(noisy_tools)
    normalized_mcp = _normalize(noisy_mcp)
    normalized_skills = _normalize(noisy_skills)
    normalized_flags = _normalize(noisy_flags)

    with_context = rng.random() < 0.5
    context_binding_ref = f"ctx:{seed}" if with_context else None
    context_content_hash = f"ctx-hash-{seed}" if with_context else None

    input_a = CapabilitySnapshotInput(
        run_id=f"run-{seed}",
        based_on_offset=seed % 17,
        tenant_policy_ref=f"policy:{seed % 5}",
        permission_mode="strict",
        tool_bindings=noisy_tools,
        mcp_bindings=noisy_mcp,
        skill_bindings=noisy_skills,
        feature_flags=noisy_flags,
        context_binding_ref=context_binding_ref,
        context_content_hash=context_content_hash,
        budget_ref=f"budget:{seed % 9}",
        quota_ref=f"quota:{seed % 7}",
        session_mode="default",
        approval_state="approved",
    )
    input_b = CapabilitySnapshotInput(
        run_id=f"run-{seed}",
        based_on_offset=seed % 17,
        tenant_policy_ref=f"policy:{seed % 5}",
        permission_mode="strict",
        tool_bindings=list(reversed(normalized_tools)),
        mcp_bindings=list(reversed(normalized_mcp)),
        skill_bindings=list(reversed(normalized_skills)),
        feature_flags=list(reversed(normalized_flags)),
        context_binding_ref=context_binding_ref,
        context_content_hash=context_content_hash,
        budget_ref=f"budget:{seed % 9}",
        quota_ref=f"quota:{seed % 7}",
        session_mode="default",
        approval_state="approved",
    )

    snapshot_a = builder.build(input_a)
    snapshot_b = builder.build(input_b)
    snapshot_c = builder.build(input_a)

    assert snapshot_a.snapshot_hash == snapshot_b.snapshot_hash
    assert snapshot_a.snapshot_hash == snapshot_c.snapshot_hash
    assert snapshot_a.tool_bindings == normalized_tools
    assert snapshot_a.mcp_bindings == normalized_mcp
    assert snapshot_a.skill_bindings == normalized_skills
    assert snapshot_a.feature_flags == normalized_flags
