# QUICKSTART

面向平台接入方的最短路径文档：安装、启动、生命周期调用、计划/审批、观测与健康检查。

## 1. 安装

```bash
pip install -e ".[dev]"
```

如果使用 Temporal substrate：

```bash
pip install temporalio
```

## 2. 启动 Runtime

### 2.1 默认（Temporal SDK 模式）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig

config = KernelRuntimeConfig(
    temporal_address="localhost:7233",
    temporal_namespace="default",
    task_queue="agent-kernel",
)

kernel = await KernelRuntime.start(config)
```

### 2.2 Temporal Host 模式（本地自举）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.substrate.temporal.adaptor import TemporalSubstrateConfig

config = KernelRuntimeConfig(
    substrate=TemporalSubstrateConfig(mode="host"),
)

kernel = await KernelRuntime.start(config)
```

### 2.3 LocalFSM 模式（无外部 Temporal）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.substrate.local.adaptor import LocalSubstrateConfig

config = KernelRuntimeConfig(
    substrate=LocalSubstrateConfig(strict_mode_enabled=False),
)

kernel = await KernelRuntime.start(config)
```

### 2.4 关闭

```python
await kernel.stop()
```

推荐使用上下文管理器自动关闭：

```python
async with await KernelRuntime.start(KernelRuntimeConfig()) as kernel:
    ...
```

## 3. 最小生命周期调用

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.kernel.contracts import StartRunRequest, SignalRunRequest, QueryRunRequest

async with await KernelRuntime.start(KernelRuntimeConfig()) as kernel:
    started = await kernel.facade.start_run(
        StartRunRequest(
            initiator="user",
            run_kind="task",
            input_json={"run_id": "run-quickstart-1"},
        )
    )

    await kernel.facade.signal_run(
        SignalRunRequest(
            run_id=started.run_id,
            signal_type="resume_from_snapshot",
            signal_payload={"snapshot_id": "snapshot-1"},
        )
    )

    view = await kernel.facade.query_run(QueryRunRequest(run_id=started.run_id))
    print(view.lifecycle_state, view.projected_offset)
```

## 4. 计划与审批示例

### 4.1 提交计划

```python
from agent_kernel.kernel.contracts import Action, SequentialPlan

plan = SequentialPlan(
    steps=(
        Action(
            action_id="a1",
            run_id="run-quickstart-1",
            action_type="tool_call",
            effect_class="read_only",
        ),
    )
)

resp = await kernel.facade.submit_plan("run-quickstart-1", plan)
print(resp.accepted, resp.plan_type)
```

### 4.2 打开与提交人工审批

```python
from agent_kernel.kernel.contracts import HumanGateRequest, ApprovalRequest

await kernel.facade.open_human_gate(
    HumanGateRequest(
        run_id="run-quickstart-1",
        gate_ref="approval-gate-1",
        gate_type="approval",
        trigger_reason="high_risk_effect",
        trigger_source="policy",
    )
)

await kernel.facade.submit_approval(
    ApprovalRequest(
        run_id="run-quickstart-1",
        approval_ref="approval-gate-1",
        approved=True,
        reviewer_id="ops-reviewer-1",
        reason="approved_after_check",
    )
)
```

## 5. Trace / Task 能力示例

```python
trace_view = await kernel.facade.query_trace_runtime("run-quickstart-1")
print(trace_view.run_state, trace_view.review_state)

health = kernel.facade.get_health()
readiness = kernel.facade.get_health_readiness()
print(health, readiness)
```

## 6. 调试与观测建议

1. 使用 `stream_run_events(run_id)` 观察权威事件流。
2. 使用 `query_run_dashboard(run_id)` 获取 dashboard 聚合字段。
3. 平台启动时调用一次 `get_manifest()` 做能力协商缓存。

## 7. 质量检查

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```

## 8. 进一步阅读

- 架构与调用关系图：[ARCHITECTURE.md](./ARCHITECTURE.md)
- 完整接口说明：[INTERFACES.md](./INTERFACES.md)
