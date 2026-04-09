"""Public test utilities for downstream consumers.

Re-exports in-memory implementations suitable for unit testing
agent-kernel integrations without requiring Temporal or a database.
"""

from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)

__all__ = [
    "AsyncExecutorService",
    "InMemoryDecisionDeduper",
    "InMemoryDecisionProjectionService",
    "InMemoryDedupeStore",
    "InMemoryKernelRuntimeEventLog",
    "StaticDispatchAdmissionService",
    "StaticRecoveryGateService",
]
