# QUICKSTART

面向平台接入方的最短路径文档：安装、启动、生命周期调用、计划/审批、观测与健康检查。

如果你只想先跑通一个最小闭环，建议直接按下面顺序读：
1. 第 2 节，选一个 substrate 启动 runtime。
2. 第 3 节，启动 run 并查询状态。
3. 第 6 节，补上基础观测。

## 1. 安装

```bash
pip install -e ".[dev]"
```

如果使用 Temporal substrate：

```bash
pip install temporalio
```

## 2. 启动 Runtime

选择建议：
- 接真实环境：用 `Temporal SDK` 模式。
- 本地单机联调：用 `Temporal Host` 模式。
- 单元测试或最小嵌入：用 `LocalFSM` 模式。

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

这段代码做了三件事：
- 创建 run。
- 给 run 发一个恢复类 signal。
- 读取标准状态视图，确认 run 当前推进到哪里。

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

什么时候需要 `submit_plan(...)`：
- 你已经在上层生成了显式计划，而不是让内核即时决定下一步。
- 你希望把计划本身也记录成 run 生命周期的一部分。

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

什么时候需要人工 gate：
- 某一步会产生高风险副作用。
- 需要人工确认是否继续。
- 需要把审批动作纳入 run 审计链路。

## 5. Trace / Task 能力示例

```python
trace_view = await kernel.facade.query_trace_runtime("run-quickstart-1")
print(trace_view.run_state, trace_view.review_state)

health = kernel.facade.get_health()
readiness = kernel.facade.get_health_readiness()
print(health, readiness)
```

这部分主要面向两类场景：
- 平台页面要展示 run 当前卡在哪个 stage / review 状态。
- 运维或研发要快速判断“内核活着”与“内核可服务”是不是同一回事。

## 6. 调试与观测建议

1. 使用 `stream_run_events(run_id)` 观察权威事件流。
2. 使用 `query_run_dashboard(run_id)` 获取 dashboard 聚合字段。
3. 平台启动时调用一次 `get_manifest()` 做能力协商缓存。

排查时的推荐顺序：
1. 先看 `query_run(...)`，确认当前投影视图。
2. 再看 `stream_run_events(...)`，确认事件推进链路。
3. 如果怀疑能力不匹配，再看 `get_manifest()`。

## 7. 质量检查

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```

## 8. 进一步阅读

- 架构与调用关系图：[ARCHITECTURE.md](./ARCHITECTURE.md)
- 完整接口说明：[INTERFACES.md](./INTERFACES.md)

常见疑问：
- 为什么已经发了 signal，还要再 query 一次：因为 signal 是输入，query 读到的 projection 才是当前稳定状态。
- 为什么有 `get_health()` 和 `get_health_readiness()` 两个接口：前者回答“活着吗”，后者回答“现在能正常接活吗”。
