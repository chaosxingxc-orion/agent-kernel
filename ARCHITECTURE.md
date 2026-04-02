# agent-kernel 完整架构图

> **0.1 Baseline** — 六权威生命周期协议 · Temporal 持久执行基底 · 单系统运行时
>
> 测试覆盖：6 235 通过（2026-04-03）

---

## 全局架构

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  PLATFORM LAYER  （平台层 — 内核不拥有，通过 Protocol 边界交互）                          ║
║                                                                                      ║
║  agent-core Runner │ REST / gRPC Gateway │ Scheduler │ Human Review UI              ║
║                                                                                      ║
║  ┌───────────────────────────────────────────────────────────────────────────────┐   ║
║  │  EventExportPort  （进化层 — 平台拥有，内核不依赖）                               │   ║
║  │                                                                               │   ║
║  │  InMemoryRunTraceStore ← dev / test                                           │   ║
║  │  OTLPRunTraceExporter  ← Jaeger / Honeycomb / Datadog (opentelemetry-api)     │   ║
║  │  <custom>              ← Kafka / S3 / Redis Streams                           │   ║
║  │                                                                               │   ║
║  │  每个 ActionCommit 写入后异步 fire-and-forget 导出；导出失败不阻塞内核执行           │   ║
║  └───────────────────────────────────────────────────────────────────────────────┘   ║
╚══════════════════════════════╦═════════════════════════════════════════════════════╝
                               ║  StartRunRequest / SignalRunRequest / QueryRunRequest
                               ║  (frozen DTO — 无平台类型泄漏)
                               ▼
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  KERNEL BOUNDARY                                                                     ║
║                                                                                      ║
║  ┌──────────────────────────────────────────────────────────────────────────────┐   ║
║  │  KernelFacade  ◄── 唯一允许的平台入口；platform 层不得直接访问任何内核组件          │   ║
║  │                                                                              │   ║
║  │  start_run() / signal_run() / cancel_run() / query_run()                    │   ║
║  │  resume_run() / spawn_child() / stream_events() / query_dashboard()         │   ║
║  └────────────────────────────────┬─────────────────────────────────────────────┘   ║
║                                   │                                                  ║
║  ┌────────────────────────────────▼─────────────────────────────────────────────┐   ║
║  │  KernelRuntime  （单系统入口）                                                   │   ║
║  │                                                                              │   ║
║  │  async with await KernelRuntime.start(config) as kernel:                    │   ║
║  │      ├── TemporalSDKWorkflowGateway  ─── Temporal 抽象层（可替换）              │   ║
║  │      ├── KernelHealthProbe  ─────────── K8s liveness / readiness             │   ║
║  │      ├── worker_task  ────────────────── asyncio background task（内核拥有）   │   ║
║  │      │       └── done_callback ──────── 失败自动传播，不无声吞掉               │   ║
║  │      └── RunActorDependencyBundle  ──── 共享服务实例（event_log 唯一来源）      │   ║
║  │                                                                              │   ║
║  │  KernelRuntimeConfig 可选项：                                                  │   ║
║  │      event_export_port   ← EventExportPort 实现（进化层导出）                   │   ║
║  │      observability_hook  ← ObservabilityHook 实现（FSM 转换监控）              │   ║
║  │      event_log_backend   ← "in_memory" | "sqlite"                           │   ║
║  │      strict_mode_enabled ← 强制 capability_snapshot_input 校验               │   ║
║  └────────────────────────────────┬─────────────────────────────────────────────┘   ║
╚══════════════════════════════════╦═════════════════════════════════════════════════╝
                                   ║ start_workflow / signal_workflow / query_projection
                                   ▼
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  TEMPORAL SERVER  （持久执行基底 — 内核 substrate，不是 business truth）                ║
║                                                                                      ║
║  Workflow History DB │ Task Queues │ Timers │ Signal Delivery │ Replay Engine         ║
╚══════════════════════════════════╦═════════════════════════════════════════════════╝
                                   ║ worker polls → dispatch workflow tasks
                  ┌────────────────╩─────────────────┐
                  │                                   │
           Run A  ▼                            Run B  ▼         ··· Run N
╔════════════════════════════╗   ╔════════════════════════════╗
║  RunActorWorkflow          ║   ║  RunActorWorkflow          ║    并行运行，
║  Authority 1: LIFECYCLE    ║   ║  Authority 1: LIFECYCLE    ║    每个 run 独立
║                            ║   ║                            ║    workflow 实例
║  run_id / projection       ║   ║  run_id / projection       ║
║  pending_signals (queue)   ║   ║  pending_signals (queue)   ║
║  signal dedup token set    ║   ║  signal dedup token set    ║
║                            ║   ║                            ║
║  [Temporal 保证单线程确定性  ║   ║  [Temporal 保证单线程确定性  ║
║   执行，signal 串行处理]     ║   ║   执行，signal 串行处理]     ║
╚════════════════╦═══════════╝   ╚════════════════════════════╝
                 ║ each turn triggers TurnEngine
                 ▼
╔═══════════════════════════════════════════════════════════════════════════════════╗
║  TurnEngine  （FSM 规范路径 — 内核唯一决策引擎）                                      ║
║                                                                                   ║
║  collecting ──► intent_committed ──► snapshot_built ──► admission_checked         ║
║       │                                                        │                  ║
║       │                                            ┌───────────┴────────────┐     ║
║       │                                            │                        │     ║
║       │                                     dispatch_blocked          dispatched  ║
║       │                                            │                        │     ║
║       │                                     completed_noop     dispatch_acknowledged  ║
║       │                                                             │              ║
║       │                                                      effect_recorded         ║
║       │                                                  effect_unknown             ║
║       │                                                         │                  ║
║       │                                                 recovery_pending            ║
║       └─────────────────────────────────────────────────────────┘                  ║
║                                                                                   ║
║  每个状态转换 → TurnStateEvent(state, reason, metadata) — 类型化，非 dict           ║
║              → ObservabilityHook.on_turn_state_transition()                       ║
╚═══════════════════════════════════════════════════════════════════════════════════╝
```

---

## 六权威详细结构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          六权威职责分离                                        │
│                                                                             │
│  Authority 2              Authority 3             Authority 4               │
│  ┌─────────────────┐     ┌──────────────────┐    ┌──────────────────────┐  │
│  │ RuntimeEventLog │     │ DecisionProjection│    │ DispatchAdmission    │  │
│  │                 │◄────│ Service           │    │ Service              │  │
│  │ append_commit() │     │                  │    │                      │  │
│  │ load()          │     │ catch_up()        │    │ admit(action, snap)  │  │
│  │                 │     │ readiness()       │    │  ├─ approval_state   │  │
│  │ [event truth]   │     │ get()             │    │  │  constraint check  │  │
│  │ append-only,    │     │                  │    │  ├─ permission_mode   │  │
│  │ never mutated   │     │ [projection truth]│    │  │  readonly gate     │  │
│  │                 │     │ replays:          │    │  └─ peer_run_bindings│  │
│  │ Backends:       │     │  authoritative_   │    │     whitelist        │  │
│  │  InMemory(PoC)  │     │  fact +           │    │                      │  │
│  │  SQLite(prod)   │     │  derived_         │    │ → AdmissionResult    │  │
│  │                 │     │  replayable       │    │   (reason_code       │  │
│  │ EventExporting  │     │  (never           │    │    Literal 7 values) │  │
│  │  Wrapper:       │     │  derived_         │    └──────────────────────┘  │
│  │  fire-and-forget│     │  diagnostic)      │                              │
│  │  export after   │     │                  │                              │
│  │  each commit    │     │ [raw inner log    │                              │
│  │  (platform-side)│     │  bypasses wrapper]│                              │
│  └─────────────────┘     └──────────────────┘                              │
│                                                                             │
│                    ┌────────────────────────────────┐                      │
│                    │         DedupeStore             │                      │
│                    │    (at-most-once dispatch gate) │                      │
│                    │                                 │                      │
│                    │  reserve → dispatched →         │                      │
│                    │  acknowledged / unknown_effect  │                      │
│                    │                                 │                      │
│                    │  Backends: InMemory / SQLite    │                      │
│                    │  [state machine — no reversals] │                      │
│                    └────────────────┬────────────────┘                      │
│                                     │ admitted + deduped                    │
│                                     ▼                                       │
│  Authority 5                                                                │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │  ExecutorService   （host_kind × interaction_target 双维度路由）       │    │
│  │                                                                    │    │
│  │  host_kind（执行机制）           interaction_target（交互对象）          │    │
│  │  ─────────────────────         ────────────────────────────────   │    │
│  │  local_process                 agent_peer   ← 另一个智能体内核       │    │
│  │  local_cli                     it_service   ← REST/gRPC/企业系统   │    │
│  │  cli_process                   data_system  ← DB/向量库/数据湖      │    │
│  │  in_process_python             tool_executor← MCP/函数调用/CLI      │    │
│  │  remote_service                human_actor  ← 审批/反馈/上报        │    │
│  │                                event_stream ← Kafka/pub-sub        │    │
│  │  Backends: AsyncExecutorService(PoC) / ActivityBackedExecutor(prod)│    │
│  └────────────────────────────────┬───────────────────────────────────┘    │
│                                    │ effect_unknown / exception             │
│                                    ▼                                        │
│  Authority 6                                                                │
│  ┌───────────────────────────────────────────────────────────────────┐     │
│  │  RecoveryGateService + CompensationRegistry                        │     │
│  │                                                                   │     │
│  │  Input: FailureEnvelope                                           │     │
│  │    ├── failed_stage: admission/execution/verification/...         │     │
│  │    ├── failure_class: deterministic/transient/policy/side_effect  │     │
│  │    ├── evidence_priority: external_ack > evidence_ref > inference │     │
│  │    └── retryability: retryable / non_retryable / unknown          │     │
│  │                                                                   │     │
│  │  Output: RecoveryDecision                                         │     │
│  │    ├── static_compensation  ─── CompensationRegistry 查找+执行     │     │
│  │    │     └── 无 handler → 自动降级为 abort（不发出空补偿意图）         │     │
│  │    ├── human_escalation     ─── escalation_channel_ref            │     │
│  │    └── abort                ─── run enters terminal state         │     │
│  │                                                                   │     │
│  │  CompensationRegistry:                                            │     │
│  │    register(effect_class, async_fn) / @handler decorator          │     │
│  │    execute(action) → bool (True=handled, False=no handler)        │     │
│  │                                                                   │     │
│  │  Persists to: RecoveryOutcomeStore (immutable once written)       │     │
│  └───────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 两层持久化设计

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TWO-LAYER PERSISTENCE                               │
│                                                                             │
│  操作层（内核拥有）                     进化层（平台拥有）                        │
│  ─────────────────────────           ──────────────────────────────────    │
│  目标：正确性 · TTL 有界              目标：分析 · 训练数据 · 跨 run 观测         │
│                                                                             │
│  SQLiteKernelRuntimeEventLog         EventExportPort（Protocol）             │
│  SQLiteDedupeStore                   │                                     │
│  SQLiteTurnIntentLog                 ├── InMemoryRunTraceStore (dev/test)   │
│  SQLiteRecoveryOutcomeStore          ├── OTLPRunTraceExporter (OTel spans)  │
│                                      └── <custom: Kafka / S3 / PostgreSQL>  │
│  [process-restart 幂等]                                                     │
│                                      RunTrace 视图字段：                     │
│  EventExportingEventLog              ├── turns[]  ← TurnTrace 列表          │
│  （装饰器 wrapper）                   ├── lifecycle_event_types[]            │
│       │                              ├── failure_count                      │
│       ├── inner.append() ← 操作层     ├── is_terminal / terminal_state      │
│       └── asyncio.create_task()      └── first_commit_at / last_commit_at   │
│           → safe_export()                                                   │
│           (timeout + exception isolated)                                    │
│                                                                             │
│  Projection 始终读 base_event_log（raw inner），永不读 wrapper，              │
│  确保导出失败不影响投影一致性                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 可观测性层

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OBSERVABILITY LAYER                               │
│                                                                             │
│  同步 Hook（热路径，FSM 转换触发）         异步导出（火-忘，ActionCommit 触发）   │
│  ─────────────────────────────────     ───────────────────────────────────  │
│                                                                             │
│  ObservabilityHook Protocol             EventExportPort Protocol            │
│        │                                      │                             │
│        ├── LoggingObservabilityHook           ├── InMemoryRunTraceStore      │
│        ├── OtelObservabilityHook              └── OTLPRunTraceExporter       │
│        │   (turn_transition span                  span 结构：                │
│        │    run_transition span)                   kernel.turn / kernel.lifecycle  │
│        ├── RunHeartbeatMonitor                    attributes: run_id / commit_id   │
│        └── CompositeObservabilityHook             / action_type / effect_class     │
│                                                   / interaction_target              │
│  每次 FSM 转换:                                   events: 每个 RuntimeEvent        │
│    TurnStateEvent(state, reason, metadata)        含 event_authority / offset       │
│    → ObservabilityHook.on_turn_state_transition() │                             │
│                                                  opentelemetry-api 可选依赖；     │
│  KernelSelfHeartbeat.refresh()                   缺失时实例化抛 ImportError        │
│    ├── event_log_check()                                                    │
│    └── projection_check()                                                   │
│          └──► KernelHealthProbe.liveness() / readiness()                    │
│                   → {"status": "ok/degraded/unhealthy", "checks": {...}}    │
│                   [平台层挂载到 /healthz / /readyz]                           │
│                                                                             │
│  RunHeartbeatMonitor.watchdog_once(gateway)                                 │
│    检测无心跳 run → gateway.signal_workflow("heartbeat_timeout")             │
│    → TurnEngine → Recovery 闭环                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 协议扩展层

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PROTOCOL EXTENSION LAYER                             │
│                                                                             │
│  所有跨层依赖通过 typing.Protocol 倒置，替换任何实现无需改动内核逻辑：              │
│                                                                             │
│  Contract Boundary              Kernel Impl (PoC)   Production Path        │
│  ──────────────────────────     ─────────────────   ─────────────────────  │
│  KernelRuntimeEventLog      ←── InMemory           SQLite / PostgreSQL     │
│  DecisionProjectionService  ←── InMemory           Redis / Postgres read   │
│  DispatchAdmissionService   ←── Static             Policy Engine / OPA     │
│  ExecutorService            ←── Async / Activity   Temporal Activity Pool  │
│  RecoveryGateService        ←── PlannedGate        ML Planner / Rule DSL   │
│  RecoveryOutcomeStore       ←── InMemory           SQLite / Postgres        │
│  DedupeStorePort            ←── InMemory           SQLite / Redis           │
│  TurnIntentLog              ←── InMemory           SQLite                  │
│  TemporalWorkflowGateway    ←── TemporalSDK        Mock / Test harness     │
│  ObservabilityHook          ←── Logging / OTel     Composite / Custom      │
│  EventExportPort            ←── InMemoryRunTrace   OTLPExporter / Kafka    │
│                                                                             │
│  Registry Extension Points:                                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  KERNEL_EVENT_REGISTRY          → register custom run.* event types  │  │
│  │  KERNEL_RECOVERY_MODE_REGISTRY  → register custom recovery modes     │  │
│  │  CompensationRegistry           → register effect_class → async_fn   │  │
│  │  register_event_transition()    → register FSM transitions           │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  CapabilitySnapshot V2 合约边界:                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  model_ref + model_content_hash    ← 防止模型静默替换                    │  │
│  │  memory_binding_ref + content_hash ← 防止记忆污染                        │  │
│  │  session_ref (via SessionPort)     ← 会话绑定，内核不拥有存储              │  │
│  │  peer_run_bindings[]               ← 智能体间信号授权白名单               │  │
│  │  snapshot_hash = SHA256(all fields) ← 防篡改，schema_version="2"        │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  Universal Interaction Contract (Action.interaction_target):                │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  agent_peer    ← 另一个智能体内核（A2A 或任何对等协议）                    │  │
│  │  it_service    ← REST / gRPC / GraphQL / 企业 IT 系统                  │  │
│  │  data_system   ← 数据库 / 向量库 / 数据湖 / 流平台                       │  │
│  │  tool_executor ← MCP / 函数调用 / CLI / 沙箱 / 代码执行                 │  │
│  │  human_actor   ← 审批门 / 反馈回路 / 人工上报                           │  │
│  │  event_stream  ← Kafka / Redis Streams / pub-sub / 消息队列            │  │
│  │                                                                      │  │
│  │  与 host_kind 正交：host_kind 描述执行机制，interaction_target 描述交互对象 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 并行驱动模型

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       PARALLEL EXECUTION MODEL                              │
│                                                                             │
│  单个 KernelRuntime / 单个 Temporal Worker / 无限并行 run                      │
│                                                                             │
│  KernelRuntime.start(config)                                                │
│       └── TemporalKernelWorker (asyncio task, polls task queue)             │
│                │                                                            │
│         ┌──────┴──────────────────────────────────┐                        │
│         │                                         │                        │
│    run-A workflow                           run-B workflow  ... run-N       │
│    (independent asyncio task)               (independent asyncio task)     │
│         │                                         │                        │
│    TurnEngine-A                             TurnEngine-B                   │
│         │                                         │                        │
│    shared event_log ◄───────────────────────────► shared event_log        │
│    (InMemory: append 无 await = asyncio 原子)                                │
│    (SQLite: 独立 run_id key，写不互扰)                                         │
│                                                                             │
│  Parent / Child 生命周期协议:                                                  │
│                                                                             │
│  parent-run                              child-run                         │
│      │──── signal child_spawned ──────────────────────────────►            │
│      │     (projection: active_child_runs += child_id)                     │
│      │                                                         │           │
│      │◄─── signal child_completed ────────────────────────────┘           │
│      │     (projection: active_child_runs -= child_id)                     │
│      │                                                                     │
│      │  on terminal: _terminate_active_children()                          │
│      │     └── signal cancel_requested → each active child                 │
│                                                                             │
│  PeerSignal 授权路径:                                                         │
│                                                                             │
│  run-src ──► peer_signal action ──► Admission.admit()                      │
│               input_json.target_run_id        │                            │
│                                 checks: target ∈ peer_run_bindings         │
│                                               └──► gateway.signal_workflow()│
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 信号路由图

```
外部信号类型             内核事件类型                    生命周期效果
─────────────            ─────────────────────────      ────────────────────────
callback            ──►  signal.callback                唤醒 actor，推进 TurnEngine
cancel_requested    ──►  run.cancel_requested           请求取消（projection 决策）
hard_failure        ──►  run.recovery_aborted           强制 Recovery 中止路径
timeout             ──►  run.waiting_external           标记等待外部
recovery_succeeded  ──►  run.recovery_succeeded         Recovery 完成，ready
recovery_aborted    ──►  run.recovery_aborted           Recovery 中止，aborted
heartbeat_timeout   ──►  run.recovering                 心跳看门狗 → Recovery 路径
request_human_input ──►  run.waiting_human_input        人工审批门（24h 超时）
human_input_received──►  run.ready                      人工输入接收，恢复 ready
resume_from_snapshot──►  run.resume_requested           从快照恢复
child_spawned       ──►  signal.child_spawned           父 run 记录子 run
child_completed     ──►  signal.child_completed         子 run 完成 → 父 run
```

---

## 关键不变量

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  KERNEL INVARIANTS (代码级约束，不可绕过)                                       │
│                                                                             │
│  1. 单一规范路径                                                               │
│     Platform → KernelFacade → TemporalGateway → RunActorWorkflow            │
│     → TurnEngine → Admission → Executor → Recovery                          │
│     无任何隐含短路路径                                                          │
│                                                                             │
│  2. 事件真相不可变                                                              │
│     RuntimeEventLog: append-only，commit_offset 单调递增                      │
│     authoritative_fact + derived_replayable 参与 Recovery replay            │
│     derived_diagnostic 永不参与决策路径（静默跳过，不报错）                      │
│                                                                             │
│  3. Admission 是外部副作用的唯一门                                               │
│     所有 effect_class != "read_only" 的动作必须经过 admit()                    │
│     approval_state ∈ {pending/denied/revoked/expired} → 强制 readonly         │
│                                                                             │
│  4. At-most-once dispatch (DedupeStore 状态机)                               │
│     reserved → dispatched → acknowledged / unknown_effect                  │
│     无逆转，无重入                                                             │
│                                                                             │
│  5. Recovery 不写入 EventLog                                                 │
│     RecoveryOutcomeStore 独立存储，不污染事件真相                                │
│     recovery.plan_selected 作为 derived_diagnostic 记录决策审计               │
│                                                                             │
│  6. Temporal 是 substrate，不是 business truth                                │
│     投影真相来自 RuntimeEventLog replay                                        │
│     Temporal query 仅返回内存缓存的 _last_projection                            │
│                                                                             │
│  7. 内核不拥有平台关切                                                          │
│     模型实现 / 记忆检索 / 会话存储 / 消息路由 → 平台层                             │
│     内核仅 CONTRACT with 引用 + hash（防静默替换）                               │
│                                                                             │
│  8. DTO 不可变                                                               │
│     所有内核 DTO: @dataclass(frozen=True, slots=True)                         │
│     TurnStateEvent 替代 dict[str, Any] — 类型化 FSM 事件                      │
│                                                                             │
│  9. 进化层导出不阻塞操作层                                                       │
│     EventExportingEventLog: fire-and-forget via asyncio.create_task()       │
│     Projection 读 base_event_log（raw inner），不读 export wrapper            │
│     导出超时/异常 → WARNING 日志，不影响 TurnEngine 执行路径                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 文件索引

| 文件 | 职责 |
|------|------|
| `kernel/contracts.py` | 所有 DTO、Protocol 接口、InteractionTarget、EffectClass |
| `kernel/turn_engine.py` | TurnEngine FSM + TurnStateEvent 类型化事件 |
| `kernel/minimal_runtime.py` | InMemory PoC 实现（测试 / 单进程用） |
| `kernel/capability_snapshot.py` | SHA256 快照构建器 v2 |
| `kernel/capability_snapshot_resolver.py` | approval_state 约束强制 |
| `kernel/event_registry.py` | 25+ 内核事件类型目录 |
| `kernel/dedupe_store.py` | 单调幂等状态机（5 值 HostKind） |
| `kernel/failure_evidence.py` | FailureEnvelope 证据优先链 |
| `kernel/event_export.py` | EventExportingEventLog · TurnTrace · RunTrace · InMemoryRunTraceStore |
| `kernel/recovery/gate.py` | PlannedRecoveryGateService + CompensationRegistry 集成 |
| `kernel/recovery/planner.py` | 确定性故障→恢复路由 |
| `kernel/recovery/compensation_registry.py` | effect_class → async callable 映射 |
| `kernel/persistence/sqlite_*.py` | SQLite 操作层（事件日志/幂等/恢复/意图） |
| `substrate/temporal/run_actor_workflow.py` | Temporal 工作流（生命周期 shell） |
| `substrate/temporal/gateway.py` | Temporal SDK 适配器 |
| `substrate/temporal/worker.py` | Worker 启动 + SIGTERM/SIGINT 优雅关闭 |
| `adapters/facade/kernel_facade.py` | 唯一允许的平台入口 |
| `runtime/kernel_runtime.py` | 单系统 KernelRuntime + KernelRuntimeConfig |
| `runtime/heartbeat.py` | RunHeartbeatMonitor + KernelSelfHeartbeat + HeartbeatWatchdog |
| `runtime/health.py` | KernelHealthProbe（liveness/readiness） |
| `runtime/observability_hooks.py` | OtelObservabilityHook + CompositeObservabilityHook |
| `runtime/otel_export.py` | OTLPRunTraceExporter（ActionCommit → OTel spans） |

---

## PoC 已知限制（路线图）

| 限制 | 当前状态 | 生产路径 |
|---|---|---|
| InMemory 服务不支持水平扩展 | PoC/单进程 | SQLite（单节点）→ PostgreSQL（分布式） |
| InMemory Projection 缓存不持久化 | 重启后全量重放 | 持久化 Projection Store + 增量追赶 |
| ObservabilityHook 同步调用 | 可能增加 turn 延迟 | 异步 hook + 内部队列缓冲 |
| Recovery 无补偿动作库预置 | CompensationRegistry 框架就位 | 补充 effect_class → callable 映射 |
| 多智能体协同无标准协议 | peer_run_bindings 提供钩子 | 实现 A2A 协议或自定义 agent_peer 路由 |
| OTLPRunTraceExporter 无内置 Exporter | 需调用方配置 TracerProvider | 提供 OTLP gRPC / HTTP exporter 默认配置 |
