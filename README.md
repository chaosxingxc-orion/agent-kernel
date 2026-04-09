# agent-kernel

面向长生命周期任务的智能体内核 -- 统一治理 run 生命周期、事件事实、副作用管控与失败恢复。

## 定位与边界

- **平台层**：负责接入请求、业务编排、展示和运营。平台通过 `KernelFacade` 与内核交互，不直接访问内部服务。
- **内核层（本仓库）**：负责 run 生命周期推进、事件记录、投影读取、副作用治理、恢复策略。内核不保留业务逻辑，不编排计划 -- Phase 4 已将 `PlanExecutor` 和 Plan 类型全部移至上层。
- **执行层**：负责工具、MCP、外部服务、子智能体执行。内核仅通过 `ExecutorService` 做 dispatch，不控制执行细节。

## 当前版本

| 项目 | 值 |
|------|------|
| Kernel version | `0.2.0` |
| Protocol version | `1.0.0` |
| Python | `>=3.12` |
| 构建工具 | hatchling |

## 目录概览

```text
agent_kernel/
  adapters/
    facade/kernel_facade.py       # 唯一平台入口 (KernelFacade)
    agent_core/                   # AgentCore 适配器 (context/checkpoint/runner/session/tool)
  kernel/
    contracts.py                  # 全部 DTO 与 Protocol 契约
    turn_engine.py                # FSM 状态机核心
    minimal_runtime.py            # 内存实现 (PoC/测试)
    capability_snapshot.py        # SHA256 快照构建器
    capability_snapshot_resolver.py
    dedupe_store.py               # 至多一次分发状态机
    event_registry.py             # 25+ 内核事件类型目录
    action_type_registry.py       # action_type 判别器注册
    failure_code_registry.py      # 失败码注册表
    failure_evidence.py           # 失败证据构建
    failure_mappings.py           # 失败码到恢复模式映射
    idempotency_key_policy.py     # 幂等键策略
    peer_auth.py                  # 对等信号鉴权
    replay_fidelity.py            # 重放保真度校验
    branch_monitor.py             # 分支监控
    cognitive/                    # LLM 推理网关与脚本运行时
    persistence/                  # PostgreSQL / SQLite 持久化实现
    recovery/                     # 恢复决策 (planner/gate/circuit_breaker)
    task_manager/                 # 任务注册、健康监控
  runtime/
    health.py                     # K8s 存活/就绪探针
    heartbeat.py                  # 超时看门狗
    metrics.py                    # 指标采集 (KernelMetricsCollector)
    observability_hooks.py        # 可观测性钩子协议
    otel_export.py                # OpenTelemetry 导出
    drain_coordinator.py          # 优雅排空协调器
    kernel_runtime.py             # KernelRuntime 组装 (遗留, 不推荐直接使用)
  service/
    http_server.py                # Starlette HTTP 服务, create_app / create_app_default
    auth_middleware.py            # Bearer-token 认证中间件
    openapi.py                    # OpenAPI 规范生成
    serialization.py              # 请求/响应序列化
  skills/                         # 技能运行时契约
  substrate/
    local/adaptor.py              # 纯进程内执行 (LocalWorkflowGateway)
    temporal/                     # Temporal workflow / worker / activity
  config.py                       # KernelConfig -- 集中配置
  testing.py                      # 测试工具 re-export
python_tests/                     # 全部测试
docs/                             # 设计文档
```

## 架构概览

### 六权限模型

内核严格执行六权限不可绕过原则：

```mermaid
graph TD
    RA[RunActor -- 生命周期权限]
    EL[RuntimeEventLog -- 事件事实权限]
    DP[DecisionProjectionService -- 投影权限]
    DA[DispatchAdmissionService -- 准入权限]
    EX[ExecutorService -- 执行权限]
    RG[RecoveryGateService -- 恢复权限]

    RA --> EL
    RA --> DP
    RA --> DA
    DA --> EX
    EX --> RG
```

### 核心数据流

```mermaid
graph LR
    P[Platform] --> F[KernelFacade]
    F --> G[WorkflowGateway]
    G --> R[RunActorWorkflow]
    R --> TE[TurnEngine]
    TE --> CSB[CapabilitySnapshotBuilder]
    TE --> DA[DispatchAdmission]
    TE --> DS[DedupeStore]
    TE --> EX[ExecutorService]
    TE --> RG[RecoveryGateService]
    R --> EL[EventLog]
    R --> PJ[Projection]
```

关键原则：

- **单入口**：平台只通过 `KernelFacade` 与内核交互。
- **双轨真相**：事件日志是事实源，Projection 是查询视图。
- **副作用先治理**：执行前必须通过 admission + dedupe。
- **失败显式恢复**：异常必须经过 recovery gate 决策。

## 快速启动

### HTTP 服务方式（推荐）

**Docker：**

```bash
docker build -t agent-kernel .
docker run -p 8400:8400 agent-kernel
```

**uvicorn 直接启动：**

```bash
pip install -e ".[dev]"
python -m uvicorn agent_kernel.service.http_server:create_app_default \
    --host 0.0.0.0 --port 8400
```

服务启动后访问 `GET /health/liveness` 确认存活，`GET /openapi.json` 查看完整 API 规范。

### 代码嵌入方式

**使用 `create_app_default()`（内存运行时，适合开发/测试）：**

```python
from agent_kernel.service.http_server import create_app_default
from agent_kernel.config import KernelConfig

app = create_app_default(KernelConfig(http_port=8400))
```

**直接构建 KernelFacade + LocalWorkflowGateway：**

```python
from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import StartRunRequest
from agent_kernel.substrate.local.adaptor import LocalWorkflowGateway
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
)
from agent_kernel.testing import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryDedupeStore,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)

event_log = InMemoryKernelRuntimeEventLog()
deps = RunActorDependencyBundle(
    event_log=event_log,
    projection=InMemoryDecisionProjectionService(event_log),
    admission=StaticDispatchAdmissionService(),
    executor=AsyncExecutorService(),
    recovery=StaticRecoveryGateService(),
    deduper=InMemoryDecisionDeduper(),
    dedupe_store=InMemoryDedupeStore(),
    strict_mode=RunActorStrictModeConfig(enabled=False),
)
gateway = LocalWorkflowGateway(deps)
facade = KernelFacade(workflow_gateway=gateway)

# 启动一个 run
resp = await facade.start_run(
    StartRunRequest(
        initiator="user",
        run_kind="task",
        input_json={"run_id": "run-demo-1"},
    )
)
print(resp.run_id, resp.lifecycle_state)
```

### 测试工具

`agent_kernel.testing` 导出全部内存实现，用于下游集成测试：

```python
from agent_kernel.testing import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryDedupeStore,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
```

这些实现不依赖 Temporal 或数据库，可在纯单元测试中使用。

## 配置

`KernelConfig` 是一个 frozen dataclass，支持构造器直接传参或从环境变量构建：

```python
from agent_kernel.config import KernelConfig

cfg = KernelConfig.from_env()
```

环境变量前缀为 `AGENT_KERNEL_`，主要字段：

| 环境变量 | 字段 | 默认值 | 说明 |
|----------|------|--------|------|
| `AGENT_KERNEL_HTTP_PORT` | `http_port` | `8400` | HTTP 监听端口 |
| `AGENT_KERNEL_API_KEY` | `api_key` | `None` | Bearer-token 认证密钥 |
| `AGENT_KERNEL_MAX_REQUEST_BODY_BYTES` | `max_request_body_bytes` | `1048576` | 请求体大小限制 |
| `AGENT_KERNEL_MAX_TRACKED_RUNS` | `max_tracked_runs` | `10000` | Facade 跟踪 run 上限 |
| `AGENT_KERNEL_MAX_RETAINED_RUNS` | `max_retained_runs` | `5000` | Projection 保留 run 上限 |
| `AGENT_KERNEL_MAX_TURN_CACHE_SIZE` | `max_turn_cache_size` | `5000` | LocalWorkflowGateway 缓存上限 |
| `AGENT_KERNEL_DEFAULT_MODEL_REF` | `default_model_ref` | `echo` | 默认模型引用 |
| `AGENT_KERNEL_DEFAULT_PERMISSION_MODE` | `default_permission_mode` | `strict` | 默认权限模式 |
| `AGENT_KERNEL_PHASE_TIMEOUT_S` | `phase_timeout_s` | `None` | TurnEngine 阶段超时（秒） |
| `AGENT_KERNEL_CIRCUIT_BREAKER_THRESHOLD` | `circuit_breaker_threshold` | `5` | 熔断器触发阈值 |
| `AGENT_KERNEL_CIRCUIT_BREAKER_HALF_OPEN_MS` | `circuit_breaker_half_open_ms` | `30000` | 熔断器半开窗口（毫秒） |
| `AGENT_KERNEL_HISTORY_RESET_THRESHOLD` | `history_reset_threshold` | `10000` | Temporal continue_as_new 阈值 |

完整字段列表见 `agent_kernel/config.py`。

## API 端点

HTTP 服务基于 Starlette，端点与 `KernelFacade` 方法一一对应。完整 OpenAPI 规范可通过 `GET /openapi.json` 获取。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/runs` | 启动 run (`start_run`) |
| GET | `/runs/{run_id}` | 查询 run 状态 (`query_run`) |
| GET | `/runs/{run_id}/dashboard` | 查询 run 仪表盘 (`query_run_dashboard`) |
| GET | `/runs/{run_id}/trace` | 查询运行时 trace (`query_trace_runtime`) |
| GET | `/runs/{run_id}/postmortem` | 查询 run 事后分析 (`query_run_postmortem`) |
| GET | `/runs/{run_id}/events` | SSE 流式订阅事件 (`stream_run_events`) |
| POST | `/runs/{run_id}/signal` | 发送信号 (`signal_run`) |
| POST | `/runs/{run_id}/cancel` | 取消 run (`cancel_run`) |
| POST | `/runs/{run_id}/resume` | 恢复 run (`resume_run`) |
| POST | `/runs/{run_id}/children` | 创建子 run (`spawn_child_run`) |
| GET | `/runs/{run_id}/children` | 查询子 run 列表 (`query_child_runs`) |
| POST | `/runs/{run_id}/approval` | 提交审批 (`submit_approval`) |
| POST | `/runs/{run_id}/stages/{stage_id}/open` | 打开 stage (`open_stage`) |
| PUT | `/runs/{run_id}/stages/{stage_id}/state` | 更新 stage 状态 (`mark_stage_state`) |
| POST | `/runs/{run_id}/branches` | 打开分支 (`open_branch`) |
| PUT | `/runs/{run_id}/branches/{branch_id}/state` | 更新分支状态 (`mark_branch_state`) |
| POST | `/runs/{run_id}/human-gates` | 打开人工门 (`open_human_gate`) |
| POST | `/runs/{run_id}/task-views` | 记录 task view (`record_task_view`) |
| PUT | `/task-views/{task_view_id}/decision` | 绑定 task view 到决策 (`bind_task_view_to_decision`) |
| POST | `/tasks` | 注册任务 (`register_task`) |
| GET | `/tasks/{task_id}/status` | 查询任务健康状态 (`get_task_status`) |
| GET | `/manifest` | 获取内核清单 (`get_manifest`) |
| GET | `/health/liveness` | 存活探针 |
| GET | `/health/readiness` | 就绪探针 (`get_health`) |
| GET | `/actions/{key}/state` | 查询 action 分发状态 (`get_action_state`) |
| GET | `/metrics` | 指标快照 |
| GET | `/openapi.json` | OpenAPI 规范 |

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试
python -m pytest -q python_tests/agent_kernel

# 运行单个测试文件
python -m pytest -q python_tests/agent_kernel/kernel/test_turn_engine.py

# Lint
ruff check agent_kernel/ python_tests/
ruff format agent_kernel/ python_tests/
pylint agent_kernel/

# 类型检查
pyright agent_kernel/
```

Pytest 配置在 `pyproject.toml` 中：`pythonpath = ["."]`，`testpaths = ["python_tests"]`。

## 工程质量

- **CI Workflows** (`.github/workflows/`)：
  - `ci-test.yml` -- pytest + 覆盖率 (最低 60%)
  - `ci-lint.yml` -- ruff check + pylint
  - `ci-typecheck.yml` -- pyright
- **Pre-commit hooks** (`.pre-commit-config.yaml`)：ruff check --fix + ruff format
- **代码规范**：Google Python Style Guide，ruff line-length=100，target Python 3.14

## 文档导航

- [ARCHITECTURE.md](./ARCHITECTURE.md) -- 架构分层、调用链路、状态模型
- [CLAUDE.md](./CLAUDE.md) -- Claude Code 工作指南与架构速查
- [CODING_STANDARDS.md](./CODING_STANDARDS.md) -- 代码与注释规范
