# QUICKSTART.md

> 版本：V6.4 | 适用对象：平台接入工程师、agent-core 集成团队
> 本文档覆盖：依赖安装、最小运行时装配、启动/信号/查询完整流程、错误处理手册、信号类型参考表。

---

## 1. 安装

### 基础依赖（仅 Temporal 运行时）

```bash
pip install agent-kernel
# 或从源码
pip install -e ".[dev]"
```

### 启用 openjiuwen agent-core 适配层

```bash
pip install "agent-kernel[agent-core]"
```

### 启用 OTel 可观测性 hook（可选）

```bash
pip install opentelemetry-api opentelemetry-sdk
# 内核在 opentelemetry-api 缺失时自动降级为 no-op，无需强依赖
```

---

## 2. 启动最小运行时

`KernelRuntime` 是推荐的单一入口。一次调用同时连接 Temporal、装配所有服务、启动 Worker。
内核和 Temporal Worker 共享一个生命周期，无需手动协调。

### 2.1 最简启动（内存模式）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig

config = KernelRuntimeConfig(task_queue="my-agent-queue")

async with await KernelRuntime.start(config) as kernel:
    # kernel.facade   — 唯一平台入口
    # kernel.gateway  — 直接访问 Temporal substrate
    # kernel.health   — K8s liveness/readiness probe
    await kernel.facade.start_run(request)
```

KernelRuntime 自动连接 `localhost:7233`，创建共享服务实例，启动 Worker 后台任务。
退出 `async with` 块时 Worker 自动优雅关闭。

### 2.2 SQLite 持久化（推荐生产使用）

```python
config = KernelRuntimeConfig(
    task_queue="my-agent-queue",
    temporal_address="temporal.prod:7233",
    event_log_backend="sqlite",
    sqlite_database_path="/data/agent-kernel.db",
)

async with await KernelRuntime.start(config) as kernel:
    ...
```

### 2.3 集成测试（传入 Temporal 测试环境 client）

```python
from temporalio.testing import WorkflowEnvironment

async with await WorkflowEnvironment.start_time_skipping() as env:
    async with await KernelRuntime.start(config, temporal_client=env.client) as kernel:
        await kernel.facade.start_run(request)
```

### 2.4 长期运行服务（主循环 + 健康检查）

```python
import asyncio

config = KernelRuntimeConfig(task_queue="my-agent-queue")
kernel = await KernelRuntime.start(config)

try:
    while True:
        kernel.check_worker()   # 若 Worker 异常退出则抛出，不会无声吞掉
        await asyncio.sleep(5)
except KeyboardInterrupt:
    pass
finally:
    await kernel.stop()
```

> **旧版 API（`AgentKernelRuntimeBundle` + `TemporalKernelWorker`）仍然可用**，适合需要精细控制依赖注入的场景。新接入推荐直接使用 `KernelRuntime`。

---

## 3. 启动 Run

```python
from agent_kernel.kernel.contracts import StartRunRequest

run_id = "run-user-42"
result = await bundle.gateway.start_workflow(
    StartRunRequest(
        initiator="user",
        run_kind="my-agent-task",
        input_json={"run_id": run_id, "task": "summarize document"},
    )
)
# result: {"run_id": "run-user-42", "workflow_id": "run:run-user-42"}
print(result["workflow_id"])
```

**注意**：`input_json` 必须包含 `run_id` 字段，workflow 从中提取运行标识。

---

## 4. 发送信号

所有外部事件通过 `signal_workflow` 注入内核。信号路径是六权威协议中的唯一合法外部输入通道。

### 4.1 回调信号（工具/服务执行完成）

```python
from agent_kernel.kernel.contracts import SignalRunRequest

await bundle.gateway.signal_workflow(
    run_id,
    SignalRunRequest(
        run_id=run_id,
        signal_type="callback",
        signal_payload={"result": {"status": "ok", "data": "..."}},
        caused_by="cb-action-99",  # 幂等去重 token
    ),
)
```

### 4.2 取消 Run

```python
await bundle.gateway.signal_workflow(
    run_id,
    SignalRunRequest(run_id=run_id, signal_type="cancel_requested"),
)
```

### 4.3 人机交互（等待审批/澄清）

```python
# 请求人工输入（Run 进入 waiting_human_input 状态，心跳超时 24h）
await bundle.gateway.signal_workflow(
    run_id,
    SignalRunRequest(
        run_id=run_id,
        signal_type="request_human_input",
        signal_payload={"prompt": "请确认此操作是否继续？"},
    ),
)

# 人工输入已接收（Run 恢复 ready 状态）
await bundle.gateway.signal_workflow(
    run_id,
    SignalRunRequest(
        run_id=run_id,
        signal_type="human_input_received",
        signal_payload={"approved": True},
        caused_by="human-review-001",
    ),
)
```

### 4.4 子 Run 生命周期信号

```python
# 父 Run 记录子 Run 已派生
await bundle.gateway.signal_workflow(
    parent_run_id,
    SignalRunRequest(
        run_id=parent_run_id,
        signal_type="child_spawned",
        signal_payload={"child_run_id": "run-child-1"},
        caused_by="spawn-001",
    ),
)

# 子 Run 完成后自动通知父 Run（由 RunActorWorkflow._notify_parent_child_completed 发送）
# 信号类型：child_completed，payload：{"child_run_id": "run-child-1"}
```

---

## 5. 查询 Run 状态

```python
projection = await bundle.gateway.query_projection(run_id)

print(projection.lifecycle_state)    # "ready" / "dispatching" / ...
print(projection.projected_offset)   # 单调递增事件 offset
print(projection.waiting_external)   # 是否在等待外部信号
print(projection.active_child_runs)  # 当前活跃子 Run 列表
```

### 通过 Facade 查询（推荐外部平台使用）

```python
from agent_kernel.kernel.contracts import QueryRunRequest

response = await bundle.facade.query_run(QueryRunRequest(run_id=run_id))
print(response.lifecycle_state)
print(response.recovery_mode)        # None / "static_compensation" / ...
```

---

## 6. 读取事件流

```python
events = await bundle.event_log.load(run_id, after_offset=0)
for event in events:
    print(f"[{event.commit_offset}] {event.event_type} | {event.event_authority}")
    # event_authority: "authoritative_fact" / "derived_replayable" / "derived_diagnostic"
```

**事件权威级别说明**：

| `event_authority` | 含义 | 是否参与 Recovery replay |
|---|---|---|
| `authoritative_fact` | 内核六权威产生的事实 | ✅ 是 |
| `derived_replayable` | 可从 fact 事件重新派生 | ✅ 是 |
| `derived_diagnostic` | 仅用于诊断/追踪，不参与决策 | ❌ 否 |

---

## 7. 错误处理手册

### 7.1 Admission 拒绝（`admitted=False`）

| `reason_code` | 原因 | 解决方式 |
|---|---|---|
| `permission_denied` | `permission_mode` 不允许此 `effect_class` | 检查 `CapabilitySnapshotInput.permission_mode`；`"readonly"` 不允许写类动作 |
| `quota_exceeded` | 配额超限 | 等待配额重置；联系策略管理员 |
| `policy_denied` | 策略规则拒绝 | 检查 `tenant_policy_ref` 对应策略；确认动作在策略白名单 |
| `dependency_not_ready` | 依赖服务未就绪 | 等待依赖完成，重新触发信号 |
| `stale_policy` | `CapabilitySnapshot` 已过期 | 重新构建 snapshot（`CapabilitySnapshotBuilder.build()`） |
| `idempotency_contract_insufficient` | 远程服务幂等性合约不足，不安全重试 | 改用 `human_escalation` recovery mode；或升级服务幂等性合约 |
| `peer_signal_missing_target` | `peer_signal` 动作缺少 `target_run_id` | 在 `action.input_json` 中补充 `target_run_id` |
| `peer_signal_unauthorized_target` | `target_run_id` 不在 `peer_run_bindings` 白名单 | 在 `CapabilitySnapshotInput.peer_run_bindings` 添加目标 run_id |

### 7.2 Recovery 决策

当 TurnEngine 进入 `recovery_pending` 状态时，`RecoveryGateService.decide()` 返回 `RecoveryDecision`：

```python
from agent_kernel.kernel.contracts import RecoveryDecision

# 示例：静态补偿
decision = RecoveryDecision(
    run_id=run_id,
    mode="static_compensation",
    reason="tool timeout — compensate with rollback",
    compensation_action_id="action-rollback-99",
)

# 示例：人工升级
decision = RecoveryDecision(
    run_id=run_id,
    mode="human_escalation",
    reason="irreversible side effect detected",
    escalation_channel_ref="channel:ops-slack-critical",
)

# 示例：终止
decision = RecoveryDecision(
    run_id=run_id,
    mode="abort",
    reason="fatal unrecoverable state",
)
```

**参见** `RUNBOOK_HUMAN_ESCALATION.md` 获取 `human_escalation` 处理的完整操作流程。

### 7.3 FailureEnvelope 证据优先级

证据优先级（由高到低）：`external_ack_ref > evidence_ref > local_inference`

```python
from agent_kernel.kernel.contracts import FailureEnvelope

envelope = FailureEnvelope(
    run_id=run_id,
    action_id="action-99",
    failed_stage="execution",          # admission/execution/verification/reconciliation/callback
    failed_component="remote-billing-api",
    failure_code="http_503",
    failure_class="transient",         # deterministic/transient/policy/side_effect/unknown
    retryability="retryable",
    external_ack_ref="ack:billing-req-001",  # 最高优先级
    local_inference="service unavailable",
    evidence_priority_source="external_ack_ref",
    evidence_priority_ref="ack:billing-req-001",
)
```

---

## 8. 信号类型参考表

所有发往内核的信号通过 `SignalRunRequest.signal_type` 传递。部分信号被映射为授权运行时事件类型（`run.*` 前缀），其余映射为 `signal.<name>`。

| signal_type | 映射事件类型 | 触发效果 |
|---|---|---|
| `callback` | `signal.callback` | 唤醒 actor，推进 TurnEngine |
| `cancel_requested` | `run.cancel_requested` | 请求取消 Run（projection 决定状态转换） |
| `hard_failure` | `run.recovery_aborted` | 强制进入 Recovery 中止路径 |
| `timeout` | `run.waiting_external` | 标记 Run 进入等待外部状态 |
| `recovery_succeeded` | `run.recovery_succeeded` | Recovery 完成，Run 恢复 ready |
| `recovery_aborted` | `run.recovery_aborted` | Recovery 中止，Run 进入 aborted |
| `heartbeat_timeout` | `run.recovering` | 心跳看门狗注入：Run 进入 recovering |
| `request_human_input` | `run.waiting_human_input` | 请求人工输入（心跳 24h 超时） |
| `human_input_received` | `run.ready` | 人工输入已接收，恢复 ready |
| `resume_from_snapshot` | `run.resume_requested` | 从快照恢复 |
| `child_spawned` | `signal.child_spawned` | 父 Run 记录子 Run 派生 |
| `child_completed` | `signal.child_completed` | 子 Run 完成通知父 Run |

**注意**：`signal.child_spawned` / `signal.child_completed` 由 `RunActorWorkflow` 内部处理，无需外部平台直接发送 `child_completed`（子 Run 在终止时自动通知父 Run）。

---

## 9. OTel 可观测性接入

内核通过 `ObservabilityHook` Protocol 暴露 FSM 状态转换事件。接入 OTel exporter 只需：

```python
from agent_kernel.runtime.observability_hooks import OtelObservabilityHook

# opentelemetry-api 缺失时自动降级为 no-op
otel_hook = OtelObservabilityHook()

# 注入到 RunActorDependencyBundle
configure_run_actor_dependencies(
    RunActorDependencyBundle(
        event_log=event_log,
        projection=...,
        admission=...,
        executor=...,
        recovery=...,
        deduper=...,
        observability_hook=otel_hook,  # 接入 OTel hook
    )
)
```

每次 TurnEngine FSM 转换会产生一个子 span（`agent_kernel.turn_transition`），携带属性：
- `agent_kernel.run_id`
- `agent_kernel.action_id`
- `agent_kernel.from_state` / `agent_kernel.to_state`
- `agent_kernel.turn_offset`

### 多 Hook 组合（日志 + OTel）

```python
from agent_kernel.runtime.observability_hooks import (
    CompositeObservabilityHook,
    LoggingObservabilityHook,
    OtelObservabilityHook,
)

hook = CompositeObservabilityHook(
    hooks=[LoggingObservabilityHook(), OtelObservabilityHook()]
)
```

---

## 10. CapabilitySnapshot 快速参考

```python
from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
    CapabilitySnapshotInput,
)

snapshot_input = CapabilitySnapshotInput(
    run_id="run-42",
    based_on_offset=0,
    tenant_policy_ref="policy:my-tenant-v1",
    permission_mode="strict",           # strict / permissive / readonly
    # V2 合约字段（可选）
    model_ref="gpt-4o:20241120",
    model_content_hash="sha256:...",
    memory_binding_ref="mem:user-42",
    memory_content_hash="sha256:...",
    session_ref="session:abc-123",
    peer_run_bindings=["run-child-1"],  # 授权 peer_signal 目标白名单
)

snapshot = CapabilitySnapshotBuilder.build(snapshot_input)
# snapshot.snapshot_hash — SHA256 覆盖全部字段，防止静默篡改
```

---

## 11. 健康检查接入

```python
from agent_kernel.runtime.health import KernelHealthProbe

probe = KernelHealthProbe()

# 注册自定义检查
probe.register_check("event_log", lambda: True)

# Liveness / Readiness（K8s 标准 JSON）
liveness = probe.liveness()    # {"status": "ok", ...}
readiness = probe.readiness()  # {"status": "ok", ...} / {"status": "degraded", ...}
```

将 `liveness()` / `readiness()` 挂载到平台层 HTTP 路由（如 FastAPI `/healthz` / `/readyz`）即可接入 K8s 探针。
