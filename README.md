# agent-kernel

企业级智能体执行内核，面向“可恢复、可观测、可扩展”的长期运行场景。

`agent-kernel` 的定位不是提示词框架，而是平台层与执行底座之间的**内核层**：
- 统一生命周期管理（Run 启动、信号、恢复、终止）
- 严格事件真相（append-only）与投影一致性
- 副作用前置准入（admission）与幂等治理（dedupe）
- 多 substrate 支持（Temporal / LocalFSM）

## 1. 当前版本状态

- Kernel 版本：`0.2.0`
- 接口协议：`1.0.0`
- Python：`3.14`
- 文档基于当前实现刷新日期：`2026-04-05`

## 2. 核心能力

- 六权威模型：`RunActor` / `EventLog` / `Projection` / `Admission` / `Executor` / `RecoveryGate`
- 计划执行：`Sequential`、`Parallel`、`Conditional`、`DependencyGraph`、`Speculative`
- 人机协同：`approval_submitted`、`human_gate_opened`、`trace_runtime_view`
- 健康探针：liveness/readiness
- 可观测性：事件流、trace 视图、任务视图（task view）

## 3. 快速开始

安装（开发模式）：

```bash
pip install -e ".[dev]"
```

最小启动示例：

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.kernel.contracts import StartRunRequest

config = KernelRuntimeConfig()

async with await KernelRuntime.start(config) as kernel:
    started = await kernel.facade.start_run(
        StartRunRequest(
            initiator="user",
            run_kind="task",
            input_json={"run_id": "run-demo-1"},
        )
    )
    print(started.run_id, started.lifecycle_state)
```

更多可运行示例见 [QUICKSTART.md](./QUICKSTART.md)。

## 4. 文档导航

- 架构设计： [ARCHITECTURE.md](./ARCHITECTURE.md)
- 接口与 DTO： [INTERFACES.md](./INTERFACES.md)
- 快速接入与使用： [QUICKSTART.md](./QUICKSTART.md)
- 路线图： [ROADMAP.md](./ROADMAP.md)

## 5. Substrate 模式

- `TemporalSubstrateConfig(mode="sdk")`：连接外部 Temporal 集群（生产推荐）
- `TemporalSubstrateConfig(mode="host")`：本地内嵌 Temporal dev server（开发/CI）
- `LocalSubstrateConfig(...)`：纯 in-process asyncio substrate（轻量部署/测试）

## 6. 测试与质量

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```

## 7. 设计原则

- 平台只通过 `KernelFacade` 进入内核
- DTO 全部使用不可变 dataclass（frozen + slots）
- 事件与投影分离，事件是事实，投影是可重建视图
- 恢复逻辑明确模式，不做隐式“吞错重试”
