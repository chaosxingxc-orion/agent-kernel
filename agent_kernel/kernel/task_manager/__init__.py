"""Task-level lifecycle management for agent-kernel.

Provides TaskRegistry, TaskWatchdog, RestartPolicy, and ReflectionBridge
to manage goal-oriented tasks that may span multiple Run attempts.

A 'task' is a semantic unit of work above the Run level:
- One task may require 1..N run attempts to complete
- On failure: RestartPolicy decides retry vs reflect
- On retry budget exhaustion: ReflectionBridge constructs LLM context
  for model-driven recovery (reflect_and_replace mode)
"""

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskHealthStatus,
    TaskLifecycleState,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.reflection_bridge import ReflectionBridge
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.kernel.task_manager.restart_policy import RestartDecision, RestartPolicyEngine
from agent_kernel.kernel.task_manager.watchdog import TaskWatchdog

__all__ = [
    "ReflectionBridge",
    "RestartDecision",
    "RestartPolicyEngine",
    "TaskAttempt",
    "TaskDescriptor",
    "TaskHealthStatus",
    "TaskLifecycleState",
    "TaskRegistry",
    "TaskRestartPolicy",
    "TaskWatchdog",
]
