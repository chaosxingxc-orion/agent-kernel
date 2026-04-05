# agent-kernel

`agent-kernel` 是一个面向长期任务执行的智能体内核，强调可恢复、可观测、可治理。

它不是提示词编排层，而是平台与执行底座之间的内核层，负责：
- run 生命周期权威推进
- 事件事实（append-only）与投影视图分离
- 副作用准入与幂等治理
- 多 substrate（Temporal / LocalFSM）统一抽象

## 1. 版本与范围

- Kernel 版本：`0.2.0`
- 接口协议：`1.0.0`
- Python：`>=3.14`
- 文档刷新日期：`2026-04-05`

## 2. 架构一览

```mermaid
graph LR
    P[Platform] --> F[KernelFacade]
    F --> G[WorkflowGateway]
    G --> R[RunActorWorkflow]
    R --> E[EventLog]
    R --> J[Projection]
    R --> T[TurnEngine]
    T --> A[Admission]
    T --> X[Executor]
    T --> C[RecoveryGate]
```

设计原则：
- 单入口：平台只通过 `KernelFacade` 调用内核。
- 双轨真相：事件是事实来源，projection 是查询视图。
- 执行前治理：任何副作用先经过 admission 与 dedupe。
- 恢复显式化：失败后必须经过 `RecoveryGate` 决策。

## 3. 核心能力

- 六权威模型：`RunActor` / `EventLog` / `Projection` / `Admission` / `Executor` / `RecoveryGate`
- 五类计划：`Sequential` / `Parallel` / `Conditional` / `DependencyGraph` / `Speculative`
- 人机协作：`approval_submitted`、`human_gate_opened`、`task_view`
- 运行健康：liveness/readiness 探针
- 观测能力：事件流、trace runtime view、task 生命周期事件

## 4. 快速启动

安装：

```bash
pip install -e ".[dev]"
```

最小示例：

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.kernel.contracts import StartRunRequest

async with await KernelRuntime.start(KernelRuntimeConfig()) as kernel:
    started = await kernel.facade.start_run(
        StartRunRequest(
            initiator="user",
            run_kind="task",
            input_json={"run_id": "run-demo-1"},
        )
    )
    print(started.run_id, started.lifecycle_state)
```

完整上手流程见 [QUICKSTART.md](./QUICKSTART.md)。

## 5. 文档导航

- 架构设计与调用关系图：[ARCHITECTURE.md](./ARCHITECTURE.md)
- 接口契约与 DTO：[INTERFACES.md](./INTERFACES.md)
- 接入与使用示例：[QUICKSTART.md](./QUICKSTART.md)
- 缺陷台账（规模化修复基线）：[KERNEL_SCALE_DEFECT_LEDGER.md](./KERNEL_SCALE_DEFECT_LEDGER.md)

## 6. Substrate 模式

- `TemporalSubstrateConfig(mode="sdk")`：连接外部 Temporal 集群（生产推荐）
- `TemporalSubstrateConfig(mode="host")`：本地内嵌 Temporal dev server（开发/CI）
- `LocalSubstrateConfig(...)`：纯 in-process 模式（轻量测试/嵌入）

## 7. 质量检查

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```
