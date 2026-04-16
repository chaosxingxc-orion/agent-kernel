"""End-to-end tests for remote-service idempotency verifier scenarios."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.remote_service_verifier import (
    STANDARD_SCENARIOS,
    RemoteServiceVerifier,
)


class TestRemoteServiceVerifier:
    """Verify all built-in remote-service scenarios pass."""

    @pytest.fixture
    def verifier(self) -> RemoteServiceVerifier:
        """Verifies a remote service contract in tests."""
        return RemoteServiceVerifier(dedupe_store=InMemoryDedupeStore())

    def test_all_standard_scenarios_pass(self, verifier: RemoteServiceVerifier) -> None:
        """Verifies all standard scenarios pass."""
        results = verifier.run_all()
        for result in results:
            assert result.passed, f"{result.scenario_name}: {result.failure_reason}"
        assert len(results) == len(STANDARD_SCENARIOS)

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS, ids=lambda s: s.name)
    def test_each_scenario_passes_in_isolation(self, scenario) -> None:
        """Verifies each scenario passes in isolation."""
        verifier = RemoteServiceVerifier(dedupe_store=InMemoryDedupeStore())
        result = verifier._run_one(scenario)
        assert result.passed, f"{scenario.name}: {result.failure_reason}"
