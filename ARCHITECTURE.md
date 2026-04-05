# ARCHITECTURE

本文档描述 `agent-kernel` 当前实现的系统架构，不包含“未来可能”设计，仅记录可落地行为。

## 1. 系统分层

### 1.1 Platform Layer

平台层（UI、网关、调度器、外部系统）只通过 `KernelFacade` 与内核交互。

### 1.2 Kernel Boundary

- `KernelFacade`：唯一平台入口
- `KernelRuntime`：统一生命周期入口（组装依赖 + 启停 substrate）
- `KernelHealthProbe`：健康检查聚合

### 1.3 Runtime Core

- `RunActorWorkflow`：单 run 生命周期 orchestrator
- `TurnEngine`：执行回合状态机
- `PlanExecutor`：多计划模型执行器

### 1.4 Substrate Layer

- `TemporalAdaptor`（sdk/host）
- `LocalFSMAdaptor`（in-process）

## 2. 六权威模型

`agent-kernel` 通过六个“不可越权组件”保证执行一致性：

1. `RunActor`：生命周期权威
2. `KernelRuntimeEventLog`：事件事实权威（append-only）
3. `DecisionProjectionService`：投影视图权威（由事件重建）
4. `DispatchAdmissionService`：副作用前置准入权威
5. `ExecutorService`：外部执行权威
6. `RecoveryGateService`：恢复决策权威

约束：
- 任何组件不得绕过 `Admission` 直接触发外部副作用
- 任何恢复动作必须通过 `RecoveryGate` 决策
- 任何状态读取以 projection 为准，不直接拼接内部临时状态

## 3. 生命周期主链路

1. 平台调用 `KernelFacade.start_run()`
2. facade 转发到 gateway `start_workflow()`
3. `RunActorWorkflow.run()` 初始化 run 上下文与 projection
4. 外部输入统一走 `signal_run()` -> `signal_workflow()`
5. workflow 将 signal 落为权威事件，驱动 `process_action_commit()`
6. `TurnEngine` 执行一轮决策/准入/执行/恢复
7. 结果写回 event log，projection catch-up 更新可查询视图

## 4. 计划执行模型

已支持的执行计划类型：
- `SequentialPlan`
- `ParallelPlan`
- `ConditionalPlan`
- `DependencyGraph`
- `SpeculativePlan`

`KernelFacade.submit_plan()` 在 facade 层做 plan_type 校验后，序列化计划并发出 `plan_submitted` 信号。workflow 侧反序列化并调用 `PlanExecutor.execute_plan()`（若已注入 plan executor）。

## 5. 幂等与一致性

### 5.1 事件与回放

- 事件日志是事实来源
- replay 支持重建 task 状态
- 对 `task.attempt_started` 做 replay 去重，避免重复回放产生 attempt 膨胀

### 5.2 Task 状态一致性

- `complete_attempt()` 仅在匹配 active run_id 时推进生命周期
- 错误 run_id 不允许“误完成”任务
- 重试失败时，决策动作与任务最终状态一致（避免 action=retry 但状态=aborted）

## 6. 可观测性与运维

### 6.1 健康探针

`KernelRuntime` 注入 `KernelHealthProbe`，当前默认挂载：
- `event_log` 可达性
- `worker` 存活性

并通过 facade 暴露：
- `get_health()`（liveness）
- `get_health_readiness()`（readiness）

### 6.2 内存后端风险提示

`InMemoryKernelRuntimeEventLog` 在 run 数超过保留阈值时会逐出最老 run，并显式输出 warning。生产建议使用持久化后端（例如 SQLite）。

## 7. Substrate 选择建议

- 生产环境：`TemporalSubstrateConfig(mode="sdk")`
- 本地开发/CI：`TemporalSubstrateConfig(mode="host")`
- 轻量嵌入测试：`LocalSubstrateConfig`

## 8. 扩展点

- Action/Plan/Recovery/Event 注册表
- Observability Hook
- Event export port
- Task view log / dedupe store / checkpoint adapter / context adapter

## 9. 已知边界

- LocalFSM 不提供跨进程隔离与 Temporal 历史能力
- Temporal 集成测试依赖本机策略允许测试 server 启动
- In-memory 后端不适合长期生产审计留存
