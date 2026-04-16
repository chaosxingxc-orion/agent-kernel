"""Contract-first tests for the agent_kernel skill interfaces."""

from __future__ import annotations

from dataclasses import fields

import agent_kernel.skills.contracts as skill_contracts


def test_skill_request_and_execution_input_match_interface_contract() -> None:
    """Skill invocation DTOs must preserve documented request fields."""
    assert [field.name for field in fields(skill_contracts.SkillRequest)] == [
        "run_id",
        "action_id",
        "skill_id",
        "skill_version",
        "input_ref",
        "input_json",
        "context_ref",
        "grant_ref",
        "caused_by",
    ]
    assert [field.name for field in fields(skill_contracts.SkillExecutionInput)] == [
        "action_id",
        "run_id",
        "action_type",
        "input_ref",
        "input_json",
        "context_ref",
        "preferred_skill_id",
        "preferred_skill_version",
        "grant_ref",
    ]


def test_managed_skill_runtime_extension_is_defined() -> None:
    """ManagedSkillRuntime should expose validate/warmup/shutdown hooks."""
    managed_runtime = skill_contracts.ManagedSkillRuntime
    for method_name in ("execute", "validate", "warmup", "shutdown"):
        assert hasattr(managed_runtime, method_name), method_name


def test_resolved_skill_plan_contract_contains_runtime_binding_fields() -> None:
    """ResolvedSkillPlan should include host and snapshot/idempotency bindings."""
    assert [field.name for field in fields(skill_contracts.ResolvedSkillPlan)] == [
        "skill",
        "host_kind",
        "grant_ref",
        "capability_snapshot_ref",
        "capability_snapshot_hash",
        "idempotency_envelope",
    ]


def test_skill_runtime_host_factories_are_defined() -> None:
    """Skill host-specific factory protocols should be available."""
    for protocol, methods in (
        (skill_contracts.SkillRuntimeHostFactory, ("create_for_host",)),
        (
            skill_contracts.LocalSkillRuntimeFactory,
            ("create_cli_process", "create_in_process_python"),
        ),
        (skill_contracts.RemoteSkillGatewayFactory, ("create_remote_service",)),
    ):
        for method_name in methods:
            assert hasattr(protocol, method_name), method_name
