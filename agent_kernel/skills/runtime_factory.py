"""Minimal host-aware runtime factory implementations for skills."""

from __future__ import annotations

from dataclasses import dataclass

from agent_kernel.skills.contracts import (
    LocalSkillRuntimeFactory,
    RemoteSkillGatewayFactory,
    SkillDefinition,
    SkillRequest,
    SkillResult,
    SkillRuntime,
    SkillRuntimeHost,
    SkillRuntimeHostFactory,
)


@dataclass(slots=True)
class _HostEchoSkillRuntime(SkillRuntime):
    """Simple runtime that echoes host information into output payload."""

    definition: SkillDefinition
    host_kind: SkillRuntimeHost

    async def execute(self, request: SkillRequest) -> SkillResult:
        """Executes request and returns deterministic success payload.
        Args:
            request: (description)
        Returns:
            SkillResult: (description)
        """
        return SkillResult(
            skill_id=self.definition.skill_id,
            action_id=request.action_id,
            success=True,
            output_json={
                "host_kind": self.host_kind,
                "skill_id": self.definition.skill_id,
                "skill_version": self.definition.version,
                "run_id": request.run_id,
            },
            evidence_ref=f"runtime:{self.host_kind}:{request.action_id}",
        )


class DefaultSkillRuntimeFactory(
    SkillRuntimeHostFactory,
    LocalSkillRuntimeFactory,
    RemoteSkillGatewayFactory,
):
    """Default host-aware skill runtime factory for minimal runtime flows."""

    async def create_for_host(
        self,
        definition: SkillDefinition,
        host_kind: SkillRuntimeHost,
    ) -> SkillRuntime:
        """Creates runtime for one host kind.
        Args:
            definition: (description)
            host_kind: (description)
        Returns:
            SkillRuntime: (description)
        Raises:
            Exception: (description)
        """
        if host_kind == "cli_process":
            return await self.create_cli_process(definition)
        if host_kind == "in_process_python":
            return await self.create_in_process_python(definition)
        if host_kind == "remote_service":
            return await self.create_remote_service(definition)
        raise ValueError(f"Unsupported skill runtime host kind: {host_kind}")

    async def create_cli_process(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime bound to CLI process host.
        Args:
            definition: (description)
        Returns:
            SkillRuntime: (description)
        """
        return _HostEchoSkillRuntime(definition=definition, host_kind="cli_process")

    async def create_in_process_python(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime bound to in-process Python host.
        Args:
            definition: (description)
        Returns:
            SkillRuntime: (description)
        """
        return _HostEchoSkillRuntime(definition=definition, host_kind="in_process_python")

    async def create_remote_service(self, definition: SkillDefinition) -> SkillRuntime:
        """Creates runtime bound to remote-service host.
        Args:
            definition: (description)
        Returns:
            SkillRuntime: (description)
        """
        return _HostEchoSkillRuntime(definition=definition, host_kind="remote_service")
