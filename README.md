# agent-kernel

> 企业级智能体执行内核 — 六权威生命周期协议 · Temporal 持久执行基底 · 单系统运行时

**6 436 测试通过** | Python 3.14 | Google Python Style Guide

---

## 什么是 agent-kernel

agent-kernel 是一个面向生产环境的智能体执行内核，解决的核心问题是：**如何让智能体的每一次动作都是可信赖的**。

它不是一个 LLM 框架，也不是 prompt 工程工具。它是位于智能体平台和 Temporal 持久执行引擎之间的**内核层**，专注于：

- **功能幂等**：每个动作精确执行一次，崩溃重启后不重复副作用
- **单实例执行**：同一个 run 在任何时刻只有一个活跃执行路径
- **可扩展 · 可进化**：所有边界通过 `typing.Protocol` 定义，任何组件可无缝替换；生产运行数据异步导出供智能体持续进化
- **可观测**：三层事件权威模型 + OpenTelemetry 导出 + K8s 健康探针
- **功能健壮 · 高可用**：结构化故障恢复（补偿/人工上报/中止）+ 心跳看门狗 + Temporal 集群 HA

---

## 核心设计：六权威协议

内核通过六个不可绕过的权威来治理每一个智能体动作的完整生命周期：

```
1. RunActor          — 生命周期权威（Temporal workflow 壳，run_actor_workflow.py）
2. RuntimeEventLog   — 事件真相权威（append-only，永不修改）
3. DecisionProjection— 投影真相权威（从事件重建状态，确定性重放）
4. DispatchAdmission — 副作用唯一门（外部副作用发生前的唯一审批点）
5. Executor          — 执行权威（与外部世界的唯一接触点）
6. RecoveryGate      — 故障恢复权威（补偿 / 人工上报 / 中止）
```

没有任何权威可以绕过另一个权威。所有跨层通信使用不可变冻结 DTO（`@dataclass(frozen=True, slots=True)`）。

---

## 架构概览

```
平台层 (Platform)
  agent-core Runner / REST Gateway / Human Review UI / OTel Backend
       │
       ▼  StartRunRequest / SignalRunRequest (frozen DTO)
  KernelFacade          ← 唯一允许的平台入口
       │
  KernelRuntime         ← 单系统入口，一次 start() 装配一切
    ├── TemporalSDKWorkflowGateway
    ├── KernelHealthProbe (liveness / readiness)
    ├── worker_task (asyncio background)
    └── RunActorDependencyBundle (共享服务实例)
       │
       ▼  Temporal 持久执行
  RunActorWorkflow → TurnEngine FSM
    ├── CapabilitySnapshot (SHA256 防篡改)
    ├── DispatchAdmission
    ├── DedupeStore (at-most-once)
    ├── Executor (host_kind × interaction_target 双维路由)
    └── RecoveryGate + CompensationRegistry
```

详细架构图见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 快速开始

### 安装依赖

```bash
pip install pytest ruff pylint
# Temporal SDK（运行时必须）
pip install temporalio
# OTel 可观测性（可选，缺失时自动降级）
pip install opentelemetry-api opentelemetry-sdk
```

### 运行测试

```bash
python -m pytest -q python_tests/agent_kernel
```

### 最小运行时

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import StartRunRequest

config = KernelRuntimeConfig(
    task_queue="my-agent-queue",
    temporal_address="localhost:7233",
)

async with await KernelRuntime.start(config) as kernel:
    response = await kernel.facade.start_run(
        StartRunRequest(initiator="user", run_kind="task")
    )
    print(response.run_id, response.lifecycle_state)
```

### SQLite 持久化（推荐生产路径）

```python
config = KernelRuntimeConfig(
    task_queue="prod-queue",
    temporal_address="temporal.prod:7233",
    event_log_backend="sqlite",
    sqlite_database_path="/data/agent_kernel.db",
)
```

### 接入 OTel 进化导出

```python
from agent_kernel.runtime.otel_export import OTLPRunTraceExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

config = KernelRuntimeConfig(
    task_queue="my-queue",
    event_export_port=OTLPRunTraceExporter(tracer_provider=provider),
)
```

### 注册补偿处理器

```python
from agent_kernel.kernel.recovery.compensation_registry import CompensationRegistry

registry = CompensationRegistry()

@registry.handler("compensatable_write", description="撤销已创建的记录")
async def undo_write(action):
    await my_store.delete(action.input_json["record_id"])

from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService
gate = PlannedRecoveryGateService(compensation_registry=registry)
```

完整集成示例见 [QUICKSTART.md](QUICKSTART.md)。

---

## 关键特性

### 功能幂等

- `DedupeStore` 状态机：`reserved → dispatched → acknowledged / unknown_effect`，单调递增，无逆转
- `IdempotencyEnvelope`：operation_fingerprint + attempt_seq + capability_snapshot_hash
- SQLite 后端：进程重启后幂等状态持续有效
- `effect_unknown` 是显式终态，不静默重试

### 单实例执行

- Temporal `WorkflowId` 唯一性在集群层面强制保证
- `RunLifecycleState` 枚举防止已 completed / aborted 的 run 被重新进入
- `RunActorDependencyBundle` Token 机制防止多 KernelRuntime 实例互相干扰

### 可扩展 · 可进化

**横向替换**（通过 Protocol 边界）：

| 接口 | PoC 实现 | 生产路径 |
|------|---------|---------|
| `KernelRuntimeEventLog` | InMemory | SQLite / PostgreSQL |
| `DecisionProjectionService` | InMemory | Redis / Postgres read |
| `ExecutorService` | AsyncExecutor | Temporal Activity Pool |
| `RecoveryGateService` | PlannedGate | ML Planner / Rule DSL |
| `EventExportPort` | InMemoryRunTraceStore | Kafka / S3 / OTel |

**纵向进化**（注册点）：
- `KERNEL_EVENT_REGISTRY` — 注册自定义事件类型
- `KERNEL_RECOVERY_MODE_REGISTRY` — 注册自定义恢复模式
- `CompensationRegistry` — 注册 `effect_class → async callable` 补偿动作
- `ObservabilityHook` — 注册 FSM 转换监听器

### 可观测

**同步 Hook**（热路径，每次 FSM 转换触发）：
- `OtelObservabilityHook` → OpenTelemetry spans
- `LoggingObservabilityHook` → 结构化日志
- `CompositeObservabilityHook` → 任意 hook 扇出

**异步导出**（火-忘，每个 ActionCommit 触发）：
- `OTLPRunTraceExporter` → 每个 ActionCommit 生成一个 OTel span，事件作为 span events
- `InMemoryRunTraceStore` → 开发 / 测试用，`RunTrace.failure_count` / `terminal_state` 等查询

**健康探针**：
- `KernelHealthProbe.liveness()` / `readiness()` → 挂载到 `/healthz` / `/readyz`
- `KernelSelfHeartbeat` → 内核自检（event log + projection 响应延迟）

### 功能健壮 · 高可用

- **三模式恢复**：`static_compensation`（已注册补偿自动执行）/ `human_escalation`（通道上报）/ `abort`（终止）
- **证据优先链**：`external_ack_ref > evidence_ref > local_inference`
- **心跳看门狗**：`RunHeartbeatMonitor.watchdog_once()` 检测无心跳 run，注入 heartbeat_timeout 信号
- **Worker 失败传播**：done_callback 自动触发，不无声吞掉 Worker 崩溃
- **Temporal 集群 HA**：Activity retry policy + task queue re-dispatch

---

## Universal Interaction Contract

`Action.interaction_target` 字段对外部交互对象进行类型化分类，与 `host_kind`（执行机制）正交：

| 值 | 说明 |
|----|------|
| `agent_peer` | 另一个智能体内核（A2A 或任何对等协议） |
| `it_service` | 传统 IT 系统（REST / gRPC / GraphQL / 企业中台） |
| `data_system` | 数据系统（数据库 / 向量库 / 数据湖 / 流平台） |
| `tool_executor` | 工具体系（MCP / 函数调用 / CLI / 沙箱） |
| `human_actor` | 人类参与者（审批门 / 反馈回路 / 人工上报） |
| `event_stream` | 消息系统（Kafka / Redis Streams / pub-sub） |

`interaction_target` 为可选字段，默认 `None`（向后兼容）。设置后可用于路由、策略决策和可观测性过滤。

---

## 文档

| 文件 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 完整架构图：全局 / 六权威 / 两层持久化 / 可观测性 / 协议扩展 |
| [QUICKSTART.md](QUICKSTART.md) | 集成快速入门：依赖 / 启动 / 信号 / 查询 / 错误处理 |
| [CLAUDE.md](CLAUDE.md) | AI 辅助开发指引（测试命令 / 架构规范 / 编码标准） |

---

## 项目结构

```
python_src/agent_kernel/
├── kernel/
│   ├── contracts.py                  # 所有 DTO + Protocol 接口
│   ├── turn_engine.py                # TurnEngine FSM（唯一决策引擎）
│   ├── minimal_runtime.py            # InMemory PoC 实现
│   ├── capability_snapshot.py        # SHA256 快照构建器 v2
│   ├── capability_snapshot_resolver.py
│   ├── event_registry.py             # 25+ 内核事件类型目录
│   ├── dedupe_store.py               # at-most-once 状态机
│   ├── failure_evidence.py           # FailureEnvelope 证据优先链
│   ├── event_export.py               # 进化层：TurnTrace / RunTrace / InMemoryRunTraceStore
│   ├── remote_service_policy.py
│   ├── persistence/
│   │   ├── sqlite_event_log.py       # SQLite 事件日志
│   │   ├── sqlite_dedupe_store.py    # SQLite 幂等状态
│   │   ├── sqlite_recovery_outcome_store.py
│   │   └── sqlite_turn_intent_log.py
│   └── recovery/
│       ├── gate.py                   # PlannedRecoveryGateService
│       ├── planner.py                # 确定性故障→恢复路由
│       └── compensation_registry.py  # effect_class → async callable
├── runtime/
│   ├── kernel_runtime.py             # 单系统入口：KernelRuntime + KernelRuntimeConfig
│   ├── health.py                     # K8s liveness / readiness
│   ├── heartbeat.py                  # 心跳看门狗
│   ├── observability_hooks.py        # OtelObservabilityHook + Composite
│   ├── otel_export.py                # OTLPRunTraceExporter
│   └── bundle.py
├── substrate/temporal/
│   ├── run_actor_workflow.py         # Temporal workflow（生命周期 shell）
│   ├── gateway.py                    # Temporal SDK 适配器
│   ├── worker.py                     # Worker 启动 + 优雅关闭
│   ├── activity_gateway.py
│   └── client.py
├── adapters/
│   ├── facade/kernel_facade.py       # 唯一平台入口
│   └── agent_core/                   # agent-core 适配层
└── skills/                           # Skills 运行时

python_tests/agent_kernel/            # 镜像 src 结构，6 235 测试
```

---

## 开发

```bash
# 运行全部测试
python -m pytest -q python_tests/agent_kernel

# 运行单个测试文件
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py

# 代码规范检查
ruff check python_src/ python_tests/
ruff format python_src/ python_tests/
pylint python_src/
```

配置见 `pyproject.toml`：`pythonpath = ["python_src"]`，目标 Python 3.14，行长 100。

---

## 已知 PoC 限制

| 限制 | 当前 | 生产路径 |
|------|------|---------|
| InMemory 服务不支持水平扩展 | 单进程 | SQLite → PostgreSQL |
| InMemory Projection 重启后重放 | 全量 | 持久化 Projection Store |
| ObservabilityHook 同步调用 | 可加延迟 | 异步 hook + 队列缓冲 |
| 补偿动作库无预置 | Registry 框架就位 | 按 effect_class 注册 callable |
| 多智能体协同无标准协议 | peer_run_bindings 钩子 | 实现 A2A 或自定义 agent_peer 路由 |
