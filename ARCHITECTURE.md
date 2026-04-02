# agent-kernel 完整架构设计

> **0.1 Baseline + 演进路线图**
>
> 标注说明：**✓ 已实现** | **○ 待实现**
>
> 测试覆盖：6 235 通过（2026-04-03）

---

## 一、总体分层

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  PLATFORM LAYER（平台层）                                                     ║
║                                                                              ║
║  agent-core Runner │ REST/gRPC Gateway │ Scheduler │ Human Review UI        ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐   ║
║  │  EventExportPort  ✓（进化层：平台拥有，内核不依赖）                      │   ║
║  │  InMemoryRunTraceStore ✓  │  OTLPRunTraceExporter ✓  │  <Kafka/S3> ○  │   ║
║  └──────────────────────────────────────────────────────────────────────┘   ║
╚══════════════════════════════╦═══════════════════════════════════════════════╝
                               ║ StartRunRequest / SignalRunRequest (frozen DTO)
                               ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║  KERNEL BOUNDARY                                                             ║
║                                                                              ║
║  KernelFacade ✓  ←── 唯一平台入口                                            ║
║  KernelRuntime ✓ ←── 单系统入口，一次 start() 装配全部服务                    ║
║       ├── TemporalSDKWorkflowGateway ✓                                       ║
║       ├── KernelHealthProbe ✓ (liveness / readiness)                        ║
║       ├── worker_task ✓ (asyncio background + done_callback)                ║
║       └── RunActorDependencyBundle ✓ (共享服务实例)                           ║
╚══════════════════════════════╦═══════════════════════════════════════════════╝
                               ║
                               ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║  COGNITIVE RUNTIME LAYER（认知运行时层）                                       ║
║                                                                              ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │  ReasoningLoop ○（待实现）                                            │    ║
║  │                                                                     │    ║
║  │  ContextPort ○ ──► ContextWindow ○ ──► LLMGateway ○                │    ║
║  │  （装配上下文）         （模型输入）       （推理基底）                  │    ║
║  │                                              │                      │    ║
║  │                                      ModelOutput ○                  │    ║
║  │                                              │                      │    ║
║  │                                      OutputParser ○                 │    ║
║  │                                      （LLM输出 → Action[]）          │    ║
║  └──────────────────────────┬──────────────────────────────────────────┘    ║
║                             │ Action[] 进入执行管道                           ║
║  ┌──────────────────────────▼──────────────────────────────────────────┐    ║
║  │  ExecutionPlan Layer ○（待实现）                                      │    ║
║  │                                                                     │    ║
║  │  SequentialPlan ○  │  ParallelPlan ○  │  PlanExecutor ○            │    ║
║  │  （串行，当前已支持）    （并行派发+聚合）    （计划解释执行器）              │    ║
║  └──────────────────────────┬──────────────────────────────────────────┘    ║
║                             │                                                ║
╚══════════════════════════════╦═══════════════════════════════════════════════╝
                               ║
                               ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║  TEMPORAL SUBSTRATE（持久执行基底）                                            ║
║                                                                              ║
║  Temporal Server: Workflow History │ Task Queues │ Timers │ Replay Engine   ║
╚══════════════════════════════╦═══════════════════════════════════════════════╝
                               ║
              ┌────────────────╩──────────────────┐
        Run A ▼                             Run B  ▼   ··· Run N
╔══════════════════════════╗  ╔══════════════════════════╗
║  RunActorWorkflow ✓      ║  ║  RunActorWorkflow ✓      ║
║  Authority 1: LIFECYCLE  ║  ║  Authority 1: LIFECYCLE  ║
║                          ║  ║                          ║
║  TurnEngine FSM ✓        ║  ║  TurnEngine FSM ✓        ║
║  （当前：串行单动作）       ║  ║                          ║
╚══════════════╦═══════════╝  ╚══════════════════════════╝
               ║
               ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║  SOUTHBOUND SUBSTRATE LAYER（南向基底层）                                      ║
║                                                                              ║
║  TemporalActivityGateway ✓                                                   ║
║    ├── execute_tool(ToolActivityInput) ✓                                    ║
║    ├── execute_mcp(MCPActivityInput) ✓                                      ║
║    ├── execute_verification(...) ✓                                          ║
║    ├── execute_reconciliation(...) ✓                                        ║
║    ├── execute_inference(InferenceActivityInput) ○  ← LLM 推理 Activity     ║
║    └── execute_skill_script(ScriptActivityInput) ○  ← 脚本执行 Activity     ║
║                                                                              ║
║  [Activity 边界：持久性在此保证]                                               ║
║                  │                            │                              ║
║         ┌────────▼────────┐         ┌─────────▼────────┐                   ║
║         │  LLMGateway ○   │         │ ScriptRuntime ○  │                   ║
║         │  (Protocol)     │         │ (Protocol)       │                   ║
║         │                 │         │                  │                   ║
║         │ infer()         │         │ execute_script() │                   ║
║         │ count_tokens()  │         │ validate_script()│                   ║
║         │ stream_infer()  │         │                  │                   ║
║         │                 │         │ host_kind 路由：  │                   ║
║         │ 职责：           │         │ local_process    │                   ║
║         │ provider 路由   │         │ in_process_python│                   ║
║         │ token 预算执行  │         │ remote_service   │                   ║
║         │ rate limit 处理 │         └──────────────────┘                   ║
║         │ 响应格式归一化   │                                                  ║
║         └────────┬────────┘                                                 ║
║                  │                                                           ║
║         Provider SDK (○ 待实现)                                              ║
║         OpenAI │ Anthropic │ Google │ Local (Ollama/vLLM)                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 二、TurnEngine FSM：当前与目标状态

```
【当前已实现 ✓】串行单动作路径：

collecting ──► intent_committed ──► snapshot_built ──► admission_checked
    │                                                         │
    │                                             ┌───────────┴────────────┐
    │                                             │                        │
    │                                      dispatch_blocked          dispatched
    │                                             │                        │
    │                                      completed_noop    dispatch_acknowledged
    │                                                              │
    │                                                      effect_recorded
    │                                                      effect_unknown
    │                                                           │
    │                                                   recovery_pending
    └───────────────────────────────────────────────────────────┘

【待实现 ○】新增状态：

推理路径（ReasoningLoop 集成后）：
  reasoning ○ ──► intent_committed（替代外部直接注入 Action）

并行执行路径：
  parallel_dispatched ○     ← 并行组已派发，等待聚合
  parallel_joined ○         ← barrier 完成，全部成功
  parallel_partial_failure ○ ← 部分分支失败，收集证据

模型反思路径：
  reflecting ○              ← 接收故障证据，模型推理反思
  （反思完成后重入 intent_committed，带修正后的 Action）
```

---

## 三、六权威详细结构（已实现 ✓）

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            六权威职责分离                                   │
│                                                                          │
│  Authority 2 ✓              Authority 3 ✓          Authority 4 ✓         │
│  ┌─────────────────┐       ┌──────────────────┐   ┌──────────────────┐  │
│  │ RuntimeEventLog │       │ DecisionProjection│   │ DispatchAdmission│  │
│  │                 │◄──────│ Service           │   │ Service          │  │
│  │ append_commit() │       │ catch_up()        │   │ admit(action,    │  │
│  │ load()          │       │ readiness()       │   │   snapshot)      │  │
│  │                 │       │ get()             │   │ approval_state   │  │
│  │ Backends:       │       │                  │   │ permission_mode  │  │
│  │  InMemory ✓     │       │ replays:          │   │ peer_run_bindings│  │
│  │  SQLite ✓       │       │  authoritative_   │   └──────────────────┘  │
│  │                 │       │  fact +           │                         │
│  │ EventExporting  │       │  derived_replayable│                         │
│  │ Wrapper ✓       │       │  (skip diagnostic)│                         │
│  └─────────────────┘       └──────────────────┘                         │
│                                                                          │
│              ┌────────────────────────────────────┐                     │
│              │  DedupeStore ✓ (at-most-once)       │                     │
│              │  reserved → dispatched →            │                     │
│              │  acknowledged / unknown_effect      │                     │
│              │  InMemory ✓ │ SQLite ✓              │                     │
│              └──────────────────┬─────────────────┘                     │
│                                 │                                        │
│  Authority 5 ✓                  ▼                                        │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  ExecutorService（host_kind × interaction_target 双维路由）         │   │
│  │                                                                  │   │
│  │  host_kind（执行机制）✓        interaction_target（交互对象）✓      │   │
│  │  local_process                 agent_peer   ← 另一个智能体内核     │   │
│  │  local_cli                     it_service   ← REST/gRPC/企业系统  │   │
│  │  cli_process                   data_system  ← DB/向量库/数据湖    │   │
│  │  in_process_python             tool_executor← MCP/函数调用/CLI    │   │
│  │  remote_service                human_actor  ← 审批/反馈/上报      │   │
│  │  [llm_inference ○]             event_stream ← Kafka/pub-sub      │   │
│  └──────────────────────────────────┬───────────────────────────────┘   │
│                                     │ effect_unknown / exception         │
│                                     ▼                                    │
│  Authority 6 ✓ + ○                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  RecoveryGateService + CompensationRegistry ✓                     │   │
│  │                                                                  │   │
│  │  RecoveryMode：                                                   │   │
│  │    static_compensation ✓ ─── CompensationRegistry 查找+执行       │   │
│  │    human_escalation ✓    ─── escalation_channel_ref              │   │
│  │    abort ✓               ─── run 进入终态                         │   │
│  │    reflect_and_retry ○   ─── 故障证据→模型反思→重新生成（待实现）    │   │
│  │                                                                  │   │
│  │  ReflectionPolicy ○：max_rounds / reflectable_failure_kinds      │   │
│  │  ScriptFailureEvidence ○：结构化故障证据供模型理解                  │   │
│  │  ReflectionContextBuilder ○：故障→ContextWindow 转换              │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 四、认知运行时层（待实现 ○）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  COGNITIVE RUNTIME LAYER                                                  │
│                                                                          │
│  上下文工程质量 → 大模型输出质量上界                                         │
│  大模型输出质量 ← f(上下文质量, 模型自身能力)   ← 两者不可互相推导            │
│                                                                          │
│  ContextPort ○（Protocol）                                                │
│       │                                                                  │
│       │  assemble(run_id, snapshot, history) → ContextWindow ○          │
│       │                                                                  │
│       │  ContextWindow 组成：                                             │
│       │    ├── 系统指令（来自 capability_scope + policy）                  │
│       │    ├── 工具定义（来自 tool_bindings，已通过 Admission 预检）        │
│       │    ├── 技能定义（来自 skill_bindings，含脚本清单）                  │
│       │    ├── 对话历史（来自 EventLog，按 token 预算裁剪）                 │
│       │    ├── 当前 run 状态（来自 ProjectionService）                     │
│       │    ├── 记忆绑定（来自 memory_binding_ref，平台层提供）              │
│       │    └── 恢复上下文（来自 RecoveryOutcomeStore，如有）               │
│       │                                                                  │
│       ▼                                                                  │
│  LLMGateway ○（Protocol，在 Temporal Activity 内部）                      │
│       │                                                                  │
│       │  infer(context, config, idempotency_key) → ModelOutput ○        │
│       │  count_tokens(context, model_ref) → int                         │
│       │  stream_infer(context, config) → AsyncIterator[Chunk]           │
│       │                                                                  │
│       │  InferenceConfig ○：                                             │
│       │    model_ref + TokenBudget ○                                    │
│       │    TokenBudget：max_input / max_output / reasoning_budget       │
│       │    temperature / stop_sequences / turn_kind_overrides ○        │
│       │    （预算按 turn 类型可配置：推理轮 vs 工具选择轮预算差异极大）       │
│       │                                                                  │
│       ▼                                                                  │
│  ModelOutput ○ → OutputParser ○（Protocol）                              │
│                       │                                                  │
│                       │  parse(output, snapshot) → list[Action]         │
│                       │  或 → ExecutionPlan ○（含并行组）                 │
│                       │                                                  │
│                       ▼                                                  │
│                  Action[] 进入 TurnEngine / PlanExecutor                  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 五、并行执行模型（待实现 ○）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PARALLEL EXECUTION MODEL                                                 │
│                                                                          │
│  ExecutionPlan ○                                                          │
│    ├── SequentialPlan ○：steps: list[Action]  （当前 TurnEngine 已支持）   │
│    └── ParallelPlan ○：groups: list[ParallelGroup]                        │
│                                                                          │
│  ParallelGroup ○：                                                        │
│    actions: list[Action]                                                 │
│    join_strategy: "all" | "any" | "n_of_m"                              │
│    n: int | None                                                         │
│    timeout_ms: int | None                                                │
│                                                                          │
│  PlanExecutor ○（在 Temporal Workflow 层）：                               │
│                                                                          │
│    SequentialPlan → 现有 TurnEngine 逐步执行                              │
│                                                                          │
│    ParallelPlan →                                                         │
│      asyncio.gather(                                                     │
│          execute_activity(action_a),  ← Temporal 原生并行                │
│          execute_activity(action_b),                                     │
│          execute_activity(action_c),                                     │
│      )                                                                   │
│                                                                          │
│    并行子智能体 →                                                          │
│      asyncio.gather(                                                     │
│          start_child_workflow(skill_a),  ← 现有 SpawnChildRunRequest    │
│          start_child_workflow(skill_b),                                  │
│          start_child_workflow(skill_c),                                  │
│      ) + barrier                                                         │
│                                                                          │
│  BranchMonitor ○（每分支独立心跳）：                                        │
│    每个 Activity / Child Workflow 有独立心跳检测                           │
│    脚本运行在子进程：wrapper 每 N 秒调 activity.heartbeat()                │
│    子进程无输出 + 耗尽超时预算 → suspected_cause = "infinite_loop"         │
│                                                                          │
│  BranchResult ○ / BranchFailure ○：                                      │
│    每个分支独立结果收集                                                    │
│    join 点汇聚：成功结果合并 + 失败证据分类                                 │
│                                                                          │
│  幂等保证：                                                                │
│    join_strategy = "all" → 组级幂等键（any 失败则整组进 Recovery）          │
│    join_strategy = "any" → 每 Action 独立幂等键（已完成的不重复执行）        │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 六、故障恢复 + 模型反思闭环（部分待实现）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  FAULT RECOVERY + REFLECTION LOOP                                         │
│                                                                          │
│  【已实现 ✓】                                                              │
│  static_compensation → CompensationRegistry.execute(action)             │
│  human_escalation    → escalation_channel_ref                           │
│  abort               → run 终态                                          │
│  FailureEnvelope ✓ 证据优先链：external_ack > evidence_ref > inference   │
│  RecoveryOutcomeStore ✓（独立存储，不污染 EventLog）                       │
│  RunHeartbeatMonitor ✓（Run 级心跳）                                      │
│                                                                          │
│  【待实现 ○】reflect_and_retry 完整闭环：                                   │
│                                                                          │
│  脚本超时（死循环）检测：                                                   │
│  Script Activity wrapper                                                 │
│    └── 子进程运行 + 每 N 秒 activity.heartbeat()                          │
│    └── 超时 → kill 子进程 → 构建 ScriptFailureEvidence ○                  │
│              ├── failure_kind = "heartbeat_timeout"                      │
│              ├── budget_consumed_ratio ≈ 1.0（死循环特征）                │
│              ├── output_produced = False（无输出特征）                    │
│              └── suspected_cause = "possible_infinite_loop"             │
│                                                                          │
│  ReflectionContextBuilder ○：                                            │
│    ScriptFailureEvidence + 原始脚本 + 成功分支结果                         │
│    → ContextWindow 增量（结构化给模型看）                                  │
│                                                                          │
│  ReflectionPolicy ○：                                                    │
│    max_rounds: int = 3                                                  │
│    reflection_timeout_ms: int                                           │
│    reflectable_failure_kinds: {heartbeat_timeout, runtime_error, ...}  │
│    non_reflectable_failure_kinds: {resource_exhausted, permission_denied}│
│    escalate_on_exhaustion: bool                                         │
│                                                                          │
│  reflect_and_retry 执行流：                                               │
│    parallel_partial_failure 事件                                         │
│        ↓ RecoveryGate 决策：reflect_and_retry                            │
│    ReflectionContextBuilder 装配上下文                                   │
│        ↓ ReasoningLoop（模型接收故障证据推理）                              │
│    模型输出：故障分析 + 修正脚本                                            │
│        ↓ 新 Action（修正后脚本）重入六权威管道                              │
│    成功 → 继续 │ 再次失败 → reflection_round + 1                          │
│        ↓ round > max_rounds                                              │
│    escalate_on_exhaustion=True  → human_escalation                      │
│    escalate_on_exhaustion=False → abort                                  │
│                                                                          │
│  reflection_round 写入 TurnIntentLog ✓（重启后不丢失计数）                 │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 七、技能子系统（部分实现）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  SKILL SUBSYSTEM                                                          │
│                                                                          │
│  【已实现 ✓】                                                              │
│  SkillDefinition ✓（skill_id / version / effect_class / tool_bindings）  │
│  SkillRuntimeHost ✓（cli_process / in_process_python / remote_service）  │
│  skill_bindings 在 CapabilitySnapshot ✓                                  │
│  SpawnChildRunRequest ✓（Child Workflow 派发基础设施）                     │
│  active_child_runs 追踪 ✓                                                │
│                                                                          │
│  【待实现 ○】技能完整驱动链路：                                             │
│                                                                          │
│  技能注入（平台→内核）：                                                    │
│    技能定义通过 ContextPort 注入 ContextWindow（脚本清单、参数 schema）○    │
│                                                                          │
│  技能调用（模型决策）：                                                     │
│    模型输出：Action(action_type="skill_call", skill_id=...) ○            │
│    经过六权威管道：Admission 检查 skill_id ∈ skill_bindings               │
│                                                                          │
│  技能生命周期（内核治理）：                                                  │
│    复杂技能 → SpawnChildRunRequest → Child RunActorWorkflow ✓            │
│    原子技能 → execute_skill_script Activity ○                            │
│                                                                          │
│  技能内脚本执行（子 Run 模型驱动）：                                         │
│    子 Run 的模型看到：技能目标 + 可用脚本列表 + 当前状态                    │
│    模型输出：Action(action_type="script_execution", script_id=...) ○    │
│    经过六权威管道 + ScriptRuntime ○ 路由到 host_kind 执行机制              │
│                                                                          │
│  串行 / 并行脚本：                                                         │
│    串行 → 当前 TurnEngine 逐 Turn 执行 ✓                                  │
│    并行 → PlanExecutor + ParallelGroup ○                                 │
│    失败反思 → reflect_and_retry ○                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 八、两层持久化（已实现 ✓）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TWO-LAYER PERSISTENCE                                                    │
│                                                                          │
│  操作层（内核拥有）✓              进化层（平台拥有）✓                        │
│  ─────────────────────           ──────────────────────────────────     │
│  SQLiteKernelRuntimeEventLog     EventExportPort（Protocol）              │
│  SQLiteDedupeStore               InMemoryRunTraceStore (dev/test)        │
│  SQLiteTurnIntentLog             OTLPRunTraceExporter (OTel spans)       │
│  SQLiteRecoveryOutcomeStore      <Kafka / S3 / PostgreSQL> ○             │
│                                                                          │
│  EventExportingEventLog（装饰器 wrapper）✓                                │
│    inner.append() ← 操作层                                               │
│    asyncio.create_task(_safe_export()) ← 火-忘导出                       │
│                                                                          │
│  Projection 始终读 base_event_log（raw inner），不读 wrapper ✓            │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 九、可观测性层（已实现 ✓）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  OBSERVABILITY LAYER                                                      │
│                                                                          │
│  同步 Hook（热路径）✓                   异步导出（火-忘）✓                  │
│  ObservabilityHook Protocol            EventExportPort Protocol          │
│    OtelObservabilityHook               OTLPRunTraceExporter              │
│    LoggingObservabilityHook            InMemoryRunTraceStore             │
│    CompositeObservabilityHook                                            │
│    RunHeartbeatMonitor                 OTel Span 结构：                  │
│                                          kernel.turn / kernel.lifecycle  │
│  KernelHealthProbe ✓                     span attrs: run_id/action_type  │
│    liveness() / readiness()              /effect_class/interaction_target│
│    → /healthz / /readyz                  span events: 每个 RuntimeEvent  │
│                                                                          │
│  KernelSelfHeartbeat ✓                                                   │
│    event_log_check() + projection_check()                               │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 十、协议扩展层（已实现 ✓）

```
Contract Boundary              Kernel Impl (PoC) ✓    Production Path
──────────────────────────     ─────────────────────   ─────────────────────
KernelRuntimeEventLog      ←── InMemory / SQLite       PostgreSQL
DecisionProjectionService  ←── InMemory                Redis / Postgres read
DispatchAdmissionService   ←── Static                  Policy Engine / OPA
ExecutorService            ←── Async / Activity        Temporal Activity Pool
RecoveryGateService        ←── PlannedGate             ML Planner / Rule DSL
RecoveryOutcomeStore       ←── InMemory / SQLite        Postgres
DedupeStorePort            ←── InMemory / SQLite        Redis
TurnIntentLog              ←── InMemory / SQLite        SQLite
TemporalWorkflowGateway    ←── TemporalSDK              Mock / Test harness
ObservabilityHook          ←── Logging / OTel           Composite / Custom
EventExportPort            ←── InMemoryRunTrace         OTLPExporter / Kafka
LLMGateway ○               ←── （待实现）                OpenAI / Anthropic
ScriptRuntime ○            ←── （待实现）                Sandbox / Python / Remote
ContextPort ○              ←── （待实现）                Platform-specific
OutputParser ○             ←── （待实现）                Tool-call / JSON mode

Registry Extension Points ✓：
  KERNEL_EVENT_REGISTRY          → custom run.* event types
  KERNEL_RECOVERY_MODE_REGISTRY  → custom recovery modes
  CompensationRegistry           → effect_class → async callable
```

---

## 十一、关键不变量

```
1. ✓ 单一规范路径：Platform → KernelFacade → Temporal → RunActorWorkflow → TurnEngine
2. ✓ 事件真相不可变：EventLog append-only，derived_diagnostic 不参与决策
3. ✓ Admission 是副作用唯一门：approve_state 约束强制 readonly
4. ✓ At-most-once dispatch：DedupeStore 单调状态机，无逆转
5. ✓ Recovery 不写 EventLog：RecoveryOutcomeStore 独立存储
6. ✓ Temporal 是 substrate：投影真相来自 EventLog replay
7. ✓ DTO 不可变：frozen=True, slots=True
8. ✓ 进化层不阻塞操作层：fire-and-forget，Projection 读 raw inner
9. ○ 上下文工程不旁路：所有模型输入经 ContextPort，不允许平台直接拼 prompt 绕过内核
10. ○ 模型推理是受治理的动作：LLM 调用有幂等键 / Token 预算 / Temporal Activity 边界
11. ○ 反思轮次有上界：ReflectionPolicy.max_rounds 防止无限反思循环
```

---

## 十二、已实现 vs 待实现全览

| 模块 | 状态 | 所在文件 |
|------|------|---------|
| 六权威协议 | ✓ | `kernel/contracts.py` + 各实现 |
| TurnEngine FSM（串行） | ✓ | `kernel/turn_engine.py` |
| KernelRuntime 单系统入口 | ✓ | `runtime/kernel_runtime.py` |
| KernelFacade 平台入口 | ✓ | `adapters/facade/kernel_facade.py` |
| CapabilitySnapshot v2 | ✓ | `kernel/capability_snapshot.py` |
| DedupeStore（InMemory+SQLite） | ✓ | `kernel/dedupe_store.py` + persistence |
| CompensationRegistry | ✓ | `kernel/recovery/compensation_registry.py` |
| EventExportPort + 导出层 | ✓ | `kernel/event_export.py` |
| OTLPRunTraceExporter | ✓ | `runtime/otel_export.py` |
| ObservabilityHook 体系 | ✓ | `runtime/observability_hooks.py` |
| KernelHealthProbe + 心跳 | ✓ | `runtime/health.py` + `heartbeat.py` |
| InteractionTarget（5类） | ✓ | `kernel/contracts.py` |
| SkillDefinition 合约 | ✓ | `skills/contracts.py` |
| **ContextPort + ContextWindow** | **○** | 待建模：`kernel/context_port.py` |
| **LLMGateway Protocol** | **○** | 待建模：`kernel/contracts.py` 扩展 |
| **InferenceConfig + TokenBudget** | **○** | 待建模：`kernel/contracts.py` 扩展 |
| **execute_inference Activity** | **○** | 待建模：`substrate/temporal/activity_gateway.py` |
| **OutputParser Protocol** | **○** | 待建模：`kernel/contracts.py` 扩展 |
| **ReasoningLoop** | **○** | 待建模：`kernel/reasoning_loop.py` |
| **ExecutionPlan + ParallelPlan** | **○** | 待建模：`kernel/contracts.py` 扩展 |
| **PlanExecutor** | **○** | 待建模：`kernel/plan_executor.py` |
| **TurnEngine 并行状态** | **○** | `kernel/turn_engine.py` 扩展 |
| **BranchMonitor** | **○** | 待建模：`kernel/branch_monitor.py` |
| **ScriptRuntime Protocol** | **○** | 待建模：`kernel/contracts.py` 扩展 |
| **execute_skill_script Activity** | **○** | 待建模：`substrate/temporal/activity_gateway.py` |
| **reflect_and_retry 恢复模式** | **○** | `kernel/contracts.py` + `recovery/` 扩展 |
| **ScriptFailureEvidence** | **○** | 待建模：`kernel/failure_evidence.py` 扩展 |
| **ReflectionPolicy** | **○** | 待建模：`kernel/recovery/reflection_policy.py` |
| **ReflectionContextBuilder** | **○** | 待建模：`kernel/recovery/reflection_builder.py` |
| **技能驱动完整链路** | **○** | `skills/` 扩展 + Child Workflow 集成 |
