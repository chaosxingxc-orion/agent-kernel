"""Task-level lifecycle management for agent-kernel.

Provides pure state-tracking components: TaskRegistry, TaskWatchdog,
contracts (TaskDescriptor, TaskAttempt, etc.), and event log.

Business logic (RestartPolicyEngine, ReflectionOrchestrator, ReflectionBridge)
has been migrated to hi-agent.
"""

from agent_kernel.kernel.task_manager.contracts import (
    ExhaustedPolicy,
    TaskAttempt,
    TaskDescriptor,
    TaskHealthStatus,
    TaskLifecycleState,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.event_log import InMemoryTaskEventLog, TaskEventAppender
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.kernel.task_manager.watchdog import TaskWatchdog

__all__ = [
    "ExhaustedPolicy",
    "InMemoryTaskEventLog",
    "TaskAttempt",
    "TaskDescriptor",
    "TaskEventAppender",
    "TaskHealthStatus",
    "TaskLifecycleState",
    "TaskRegistry",
    "TaskRestartPolicy",
    "TaskWatchdog",
]
