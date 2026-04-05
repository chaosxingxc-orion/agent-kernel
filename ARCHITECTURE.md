# ARCHITECTURE

本文档描述 `agent-kernel` 当前可执行实现的架构设计，重点覆盖：
- 设计逻辑如何演进到当前形态
- 分层职责和调用边界
- 核心状态机与调用关系
- 与规模化运行相关的约束与扩展点

阅读建议：
- 如果你是第一次接触这个仓库，先看第 1、2、4 节，建立整体心智模型。
- 如果你在做平台接入，重点看第 4、5、6、7 节。
- 如果你在做内核扩展，重点看第 3、6、8 节。

先记住一句话：
`agent-kernel` 的本质不是“执行器”，而是“带事件真相和恢复治理的 run 生命周期内核”。

## 1. 设计逻辑演进

### 1.0 为什么这个内核存在

普通的 agent 执行链在规模变大后，通常会遇到四类问题：
- 一旦进程退出，长任务状态丢失。
- 工具调用和内部状态混在一起，失败后难以判断是否已经产生副作用。
- 查询面和执行面耦合，平台很难稳定展示“当前到底进行到哪里”。
- 人工审批、外部回调、子任务并行等能力不断叠加后，逻辑迅速失控。

`agent-kernel` 的设计就是围绕这四类问题逐步收敛出来的。

### 1.1 演进目标

`agent-kernel` 的核心目标不是“封装一次模型调用”，而是提供一个可长期运行、可恢复、可治理的执行内核：
- 运行不丢（durable / restart-safe）
- 状态可重建（event -> projection）
- 副作用可控（admission + dedupe + recovery）
- 接口稳定（platform 只面向 facade）

### 1.2 演进阶段

1. 单次执行阶段（早期 PoC）
- 问题：执行链路与状态混在一起，失败后难以恢复。
- 改进方向：把生命周期、状态、执行、副作用治理拆开。

2. 权威拆分阶段（六权威）
- 引入六权威职责：`RunActor`、`EventLog`、`Projection`、`Admission`、`Executor`、`RecoveryGate`。
- 结果：事实（event）与视图（projection）分离，恢复路径可审计。

3. substrate 解耦阶段
- 将 Temporal 作为“可插拔执行底座”，通过 `TemporalWorkflowGateway` 抽象隔离 SDK 细节。
- 结果：同一套 kernel 逻辑可运行在 `Temporal(sdk/host)` 或 `LocalFSM`。

4. 协作增强阶段
- 增加 `plan_submitted` / `approval_submitted` / `speculation_committed`。
- 增加 TRACE 相关能力：branch/stage/human_gate/task_view。
- 结果：支持更复杂的人机协作与平台化观测。

## 2. 系统分层与依赖

```mermaid
graph TB
    subgraph P[Platform Layer]
      UI[UI / API Gateway / Scheduler]
    end

    subgraph K[Kernel Boundary]
      KR[KernelRuntime]
      KF[KernelFacade]
      HP[KernelHealthProbe]
    end

    subgraph C[Runtime Core]
      GW[TemporalWorkflowGateway]
      RA[RunActorWorkflow]
      TE[TurnEngine]
      PE[PlanExecutor]
    end

    subgraph A[State Authorities]
      EL[KernelRuntimeEventLog]
      PJ[DecisionProjectionService]
      AD[DispatchAdmissionService]
      EX[ExecutorService]
      RC[RecoveryGateService]
      DD[DecisionDeduper / DedupeStore]
    end

    subgraph S[Substrate]
      TA[TemporalAdaptor sdk/host]
      LA[LocalFSMAdaptor]
    end

    UI --> KF
    KR --> KF
    KR --> TA
    KR --> LA
    KF --> GW
    GW --> RA
    RA --> TE
    RA --> PE
    RA --> EL
    RA --> PJ
    TE --> AD
    TE --> EX
    TE --> RC
    TE --> DD
```

分层约束：
- 平台层禁止直接调用 substrate 或 workflow。
- 业务读状态走 `Facade.query_*`，不直接读 event log。
- 生命周期推进只由 `RunActorWorkflow` 驱动。

可以把这些层理解成两条平行主线：
- 控制线：`Platform -> Facade -> Gateway -> RunActor`
- 执行线：`RunActor -> TurnEngine -> Admission/Executor/Recovery`

前者负责“谁能发起、何时推进”；后者负责“这一轮具体怎么执行、失败后怎么办”。

## 2.1 核心术语表

| 术语 | 含义 | 为什么重要 |
|---|---|---|
| `run` | 内核管理的一个执行实例 | 所有生命周期、事件、查询都围绕 run 展开 |
| `event` | append-only 的事实记录 | 用来回放和审计，不能随意修改 |
| `projection` | 从事件重建出的当前视图 | 平台读状态时看它，而不是读内部变量 |
| `signal` | 外部输入给 run 的触发消息 | signal 不是最终真相，通常会先映射为权威事件 |
| `turn` | 一次决策-准入-执行-恢复回合 | run 是长期的，turn 是 run 内的一轮 |
| `admission` | 执行前的准入检查 | 防止高风险副作用被直接放行 |
| `recovery` | 失败后的恢复决策 | 明确补偿、人工介入或终止策略 |
| `substrate` | 承载 run 的底座实现 | 当前支持 Temporal 和 LocalFSM |

## 3. 六权威职责模型

| 权威组件 | 角色 | 关键责任 | 代码位置 |
|---|---|---|---|
| RunActor | 生命周期权威 | 驱动 run 启动、信号处理、回合推进 | `agent_kernel/substrate/temporal/run_actor_workflow.py` |
| EventLog | 事实权威 | 追加不可变事件、提供回放基线 | `agent_kernel/kernel/minimal_runtime.py` |
| Projection | 视图权威 | 从事件重建 run 视图，提供 query/readiness | `agent_kernel/kernel/minimal_runtime.py` |
| Admission | 副作用准入权威 | 执行前策略检查与准入包络 | `agent_kernel/kernel/minimal_runtime.py` |
| Executor | 执行权威 | 执行动作（tool/mcp/...）并返回结果 | `agent_kernel/kernel/minimal_runtime.py` |
| RecoveryGate | 恢复权威 | 失败后恢复决策（补偿/人工/终止） | `agent_kernel/kernel/minimal_runtime.py` |

## 4. 核心调用关系

### 4.1 Run 启动与信号处理主链路

```mermaid
sequenceDiagram
    participant PL as Platform
    participant KF as KernelFacade
    participant GW as WorkflowGateway
    participant RA as RunActorWorkflow
    participant EL as EventLog
    participant PJ as Projection
    participant TE as TurnEngine

    PL->>KF: start_run(StartRunRequest)
    KF->>GW: start_workflow(request)
    GW->>RA: run(RunInput)
    RA->>EL: append run.created / run.started
    RA->>PJ: catch_up + readiness
    KF-->>PL: StartRunResponse

    PL->>KF: signal_run(SignalRunRequest)
    KF->>GW: signal_workflow(run_id, signal)
    GW->>RA: signal(ActorSignal)
    RA->>EL: append signal.received + mapped run.* event
    RA->>RA: process_action_commit(...)
    RA->>PJ: catch_up + readiness
    RA->>TE: run_turn(...)
    TE-->>RA: TurnResult
    RA->>EL: append turn/recovery outcome events
```

这条链路最关键的设计点是：
- 任何外部输入先进入 workflow，而不是直接改 projection。
- `RunActorWorkflow` 先写事件，再推进视图，保证“先有事实，再有状态”。
- `TurnEngine` 负责一轮执行闭环，但生命周期最终仍由 `RunActorWorkflow` 持有。

### 4.2 RunActor 内部依赖调用

```mermaid
graph LR
    SIG[signal/input] --> MAP[_signal_event_type]
    MAP --> COMMIT[ActionCommit]
    COMMIT --> CATCHUP[Projection.catch_up]
    CATCHUP --> READY[Projection.readiness]
    READY --> DEDUPE[DecisionDeduper seen/mark]
    DEDUPE --> TURN[TurnEngine.run_turn]
    TURN --> ADMIT[Admission check]
    TURN --> EXEC[Executor execute]
    TURN --> REC[RecoveryGate decide]
    TURN --> APPEND[append turn/recovery events]
    APPEND --> NEXT[Next projection state]
```

这张图表达的是“一个 signal 不会直接变成一次工具调用”，中间至少要经过：
- projection catch-up：先确认当前 run 已经追平到哪个 offset。
- readiness 判断：当前状态是否允许继续推进。
- dedupe 判断：这一轮是否已经被处理过。
- admission / execution / recovery：真正的执行治理链。

### 4.3 信号到权威事件映射（关键片段）

`RunActorWorkflow` 中 `_SIGNAL_EVENT_TYPE_MAP` 将 transport signal 归一到权威事件：
- `resume_from_snapshot -> run.resume_requested`
- `cancel_requested -> run.cancel_requested`
- `timeout -> run.waiting_external`
- `hard_failure -> run.recovery_aborted`
- `plan_submitted -> run.plan_submitted`
- `approval_submitted -> run.approval_submitted`
- `speculation_committed -> run.speculation_committed`

## 5. 生命周期状态机

`RunLifecycleState` 当前值：
`created | ready | dispatching | waiting_result | waiting_external | recovering | completed | aborted`

```mermaid
stateDiagram-v2
    [*] --> created
    created --> ready: run.ready / signal.*
    ready --> dispatching: run.dispatching
    dispatching --> waiting_result: turn.dispatched
    waiting_result --> ready: run.ready / run.recovery_succeeded
    ready --> recovering: run.recovering / run.resume_requested
    recovering --> ready: run.recovery_succeeded
    recovering --> waiting_external: run.waiting_external
    waiting_external --> recovering: resume_from_snapshot
    waiting_external --> ready: external callback signal
    created --> aborted: run.cancel_requested / run.recovery_aborted
    ready --> aborted: run.cancel_requested / run.recovery_aborted
    waiting_external --> aborted: run.cancel_requested / run.recovery_aborted
    recovering --> aborted: run.recovery_aborted
    ready --> completed: run.completed
```

状态机约束：
- `run.cancel_requested` 是权威生命周期事实，不是“仅通知”。
- `completed/aborted` 后不允许低优先级运行态事件覆盖。
- `projection` 是查询真相，事件是重建来源。

一个更容易理解的状态演进故事是：
1. `created`：run 刚被接受，还没准备好执行。
2. `ready`：可以进入下一轮调度。
3. `dispatching / waiting_result`：某个动作正在被发出或等待结果。
4. `waiting_external`：需要等外部世界，比如回调、审批、恢复输入。
5. `recovering`：内核正在处理失败后的恢复策略。
6. `completed / aborted`：run 进入终态，不再接受普通推进事件。

## 6. 接口边界与一致性约束

1. 入口边界
- 平台层只通过 `KernelFacade` 进入内核。
- 不直接使用 `Temporal` SDK 对 run 写入业务信号。

2. 一致性边界
- EventLog append-only，不允许原地修改历史。
- Projection 只能由事件回放推进。
- Recovery 决策必须走 `RecoveryGateService`。

3. 副作用边界
- 任何副作用先过 `Admission`。
- 幂等状态通过 `DecisionDeduper/DedupeStore` 跟踪。
- 失败后的处理必须形成 recovery 事件闭环。

为什么要把这些边界说得这么硬：
- 如果平台可以直接改状态，那么 event replay 就不再可信。
- 如果 executor 可以绕过 admission，那么高风险副作用无法被治理。
- 如果 recovery 不写事件，那么故障后的分析和重放会失真。

## 7. Substrate 选择与取舍

| 模式 | 配置 | 优势 | 限制 | 推荐场景 |
|---|---|---|---|---|
| Temporal SDK | `TemporalSubstrateConfig(mode="sdk")` | 外部集群、持久化与稳定性最佳 | 依赖外部 Temporal 基础设施 | 生产 |
| Temporal Host | `TemporalSubstrateConfig(mode="host")` | 单机可自举、便于本地/CI | 仍需 Temporal testing 依赖 | 开发/集成测试 |
| LocalFSM | `LocalSubstrateConfig(...)` | 轻量、无外部依赖 | 无 durable history、无跨进程隔离 | 单进程测试/嵌入式场景 |

## 8. 扩展面与规模化建议

### 8.1 可扩展点
- 类型注册：`action_type` / `plan_type` / `event_type` / `recovery_mode`
- 观测扩展：`ObservabilityHook`、event export
- 数据平面扩展：event log / dedupe / task view 的持久化后端

### 8.2 大规模工程落地建议

1. 生产优先使用 `Temporal(sdk)` + 持久化 event log。
2. 平台侧在启动时缓存 `KernelManifest`，做能力协商。
3. 统一约束 signal taxonomy，避免 ad-hoc signal 语义漂移。
4. 对 `task.*`、`human_gate.*`、`branch/stage` 事件建立监控面板。
5. 将“事件 schema 版本 + 回放校验”纳入发布门禁。

常见误区：
- 把 `signal` 当成最终状态：不对，signal 更像外部刺激，真正状态要看 event replay 后的 projection。
- 把 `LocalFSM` 当成生产默认选项：不合适，它适合本地测试和轻量嵌入，不提供跨进程 durability。
- 直接在平台代码里拼 run 状态：不建议，应该统一读取 facade/query 暴露的视图。
