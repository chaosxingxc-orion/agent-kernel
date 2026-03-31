"""Tests for host-aware skill runtime factory implementations."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.skills.contracts import SkillDefinition, SkillRequest
from agent_kernel.skills.runtime_factory import DefaultSkillRuntimeFactory


def _build_skill_definition() -> SkillDefinition:
    return SkillDefinition(
        skill_id="skill.search",
        version="1.0.0",
        skill_kind="tool",
        effect_class="read_only",
        input_schema_ref="schema://input",
        output_schema_ref="schema://output",
    )


def _build_skill_request() -> SkillRequest:
    return SkillRequest(
        run_id="run-1",
        action_id="action-1",
        skill_id="skill.search",
    )


def test_factory_routes_cli_process_host() -> None:
    factory = DefaultSkillRuntimeFactory()
    runtime = asyncio.run(factory.create_for_host(_build_skill_definition(), "cli_process"))
    result = asyncio.run(runtime.execute(_build_skill_request()))
    assert result.success
    assert result.output_json is not None
    assert result.output_json["host_kind"] == "cli_process"


def test_factory_routes_in_process_python_host() -> None:
    factory = DefaultSkillRuntimeFactory()
    runtime = asyncio.run(factory.create_for_host(_build_skill_definition(), "in_process_python"))
    result = asyncio.run(runtime.execute(_build_skill_request()))
    assert result.success
    assert result.output_json is not None
    assert result.output_json["host_kind"] == "in_process_python"


def test_factory_routes_remote_service_host() -> None:
    factory = DefaultSkillRuntimeFactory()
    runtime = asyncio.run(factory.create_for_host(_build_skill_definition(), "remote_service"))
    result = asyncio.run(runtime.execute(_build_skill_request()))
    assert result.success
    assert result.output_json is not None
    assert result.output_json["host_kind"] == "remote_service"


def test_factory_rejects_unknown_host_kind() -> None:
    factory = DefaultSkillRuntimeFactory()
    with pytest.raises(ValueError, match="Unsupported skill runtime host kind"):
        asyncio.run(factory.create_for_host(_build_skill_definition(), "invalid_host"))  # type: ignore[arg-type]


def test_factory_direct_host_methods_align_with_create_for_host_route() -> None:
    """Direct factory methods should produce same host-kind runtime behavior."""
    factory = DefaultSkillRuntimeFactory()
    definition = _build_skill_definition()
    request = _build_skill_request()

    cli_runtime = asyncio.run(factory.create_cli_process(definition))
    py_runtime = asyncio.run(factory.create_in_process_python(definition))
    remote_runtime = asyncio.run(factory.create_remote_service(definition))

    cli_result = asyncio.run(cli_runtime.execute(request))
    py_result = asyncio.run(py_runtime.execute(request))
    remote_result = asyncio.run(remote_runtime.execute(request))

    assert cli_result.output_json is not None
    assert py_result.output_json is not None
    assert remote_result.output_json is not None
    assert cli_result.output_json["host_kind"] == "cli_process"
    assert py_result.output_json["host_kind"] == "in_process_python"
    assert remote_result.output_json["host_kind"] == "remote_service"


def test_factory_runtime_output_preserves_execution_identity_fields() -> None:
    """Runtime output should keep identity fields stable for downstream assertions."""
    factory = DefaultSkillRuntimeFactory()
    definition = _build_skill_definition()
    request = _build_skill_request()

    runtime = asyncio.run(factory.create_for_host(definition, "remote_service"))
    result = asyncio.run(runtime.execute(request))

    assert result.output_json is not None
    assert result.output_json["skill_id"] == "skill.search"
    assert result.output_json["skill_version"] == "1.0.0"
    assert result.output_json["run_id"] == "run-1"
    assert result.evidence_ref == "runtime:remote_service:action-1"
