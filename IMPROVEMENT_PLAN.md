# Agent-Kernel 改进计划

> 基于五维度评分体系的系统化改进路线图
> 生成日期: 2026-04-05

---

## 修订后评分总览

经过深入代码审查，修正初始评估中的两个误判后（OtelObservabilityHook 已实现、per-run asyncio.Lock 已存在于 minimal_runtime），修订后得分如下：

| 维度 | 得分 | 满分 | 百分比 |
|------|------|------|--------|
| D1 功能幂等 | 27 | 30 | 90.0% |
| D2 单实例执行 | 21 | 25 | 84.0% |
| D3 可扩展·可进化 | 33 | 35 | 94.3% |
| D4 可观测 | 27 | 30 | 90.0% |
| D5 功能健壮·高可用 | 34 | 40 | 85.0% |
| **合计** | **142** | **160** | **88.8%** |

---

## 改进优先级

### 优先级定义

| 级别 | 定义 | 目标时间 |
|------|------|----------|
| **P0** | 阻塞生产部署的架构缺陷 | 1-2 周 |
| **P1** | 降低运维信心或限制水平扩展 | 3-4 周 |
| **P2** | 提升成熟度但不阻塞交付 | 6-8 周 |

---

## P0 — 生产阻塞项

### P0-1: 持久化层可插拔抽象 + PostgreSQL 适配器

**评分影响**: D5.6 (3→5), D2.4 (3→5) → 总分 +4

**问题诊断**:
当前所有持久化组件（EventLog、DedupeStore、CircuitBreakerStore、RecoveryOutcomeStore）绑定 SQLite 单文件。
生产环境面临：
- 单写入者限制（WAL 模式仅允许一个并发写入者）
- 无法跨节点共享状态
- `threading.Lock` / `RLock` 保护无法扩展到多进程

**改造方案**:

#### 阶段 A: 提取存储端口协议 (不改现有行为)

当前 `SQLiteDedupeStore` 直接实现方法但未继承 `DedupeStorePort`，`SQLiteKernelRuntimeEventLog` 继承了 `KernelRuntimeEventLog`。需统一为 Protocol 模式。

```
新建文件:
  agent_kernel/kernel/persistence/ports.py

内容:
  class EventLogStore(Protocol):
      """持久化层事件日志端口。
      
      与 KernelRuntimeEventLog (kernel/contracts.py) 的区别：
      - KernelRuntimeEventLog 是内核级合约，定义语义行为
      - EventLogStore 是持久化层端口，定义存储操作
      
      所有实现必须保证:
      1. per-run 单调递增 offset
      2. append 的原子性（全部写入或全部不写入）
      3. read 返回按 offset 升序排列
      """
      async def append_action_commit(self, commit: ActionCommit) -> str: ...
      def read_events(self, run_id: str) -> list[RuntimeEvent]: ...
      def close(self) -> None: ...

  class DedupeStore(Protocol):
      """持久化层去重存储端口。
      
      状态机: reserved → dispatched → acknowledged/succeeded/unknown_effect
      所有转换必须单调，不可逆。
      """
      def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation: ...
      def reserve_and_dispatch(self, envelope: IdempotencyEnvelope) -> DedupeReservation: ...
      def mark_dispatched(self, key: str) -> None: ...
      def mark_acknowledged(self, key: str) -> None: ...
      def mark_succeeded(self, key: str) -> None: ...
      def mark_unknown_effect(self, key: str) -> None: ...
      def get(self, key: str) -> DedupeRecord | None: ...
      def close(self) -> None: ...

  class CircuitBreakerStore(Protocol):
      """持久化层熔断器存储端口。"""
      def get_failure_count(self, effect_class: str) -> int: ...
      def increment_failure(self, effect_class: str) -> int: ...
      def reset(self, effect_class: str) -> None: ...
      def get_last_failure_ts(self, effect_class: str) -> int | None: ...
      def close(self) -> None: ...
```

#### 阶段 B: PostgreSQL 适配器

```
新建文件:
  agent_kernel/kernel/persistence/pg_event_log.py
  agent_kernel/kernel/persistence/pg_dedupe_store.py
  agent_kernel/kernel/persistence/pg_circuit_breaker_store.py
  agent_kernel/kernel/persistence/pg_recovery_outcome_store.py
  agent_kernel/kernel/persistence/pg_colocated_bundle.py

依赖: asyncpg (异步 PostgreSQL 驱动)

关键设计决策:
  1. 连接池: asyncpg.create_pool(min_size=2, max_size=10)
  2. DedupeStore.reserve: 
     INSERT INTO dedupe_records (key, state, ...) 
     VALUES ($1, 'reserved', ...) 
     ON CONFLICT (key) DO NOTHING 
     RETURNING state
     -- 利用 PG 原生 UPSERT 替代应用层锁
  3. EventLog.append: 
     使用 advisory lock (pg_advisory_xact_lock) per run_id 
     保证 per-run 串行写入，替代 threading.Lock
  4. CircuitBreakerStore: 
     INSERT ... ON CONFLICT DO UPDATE SET 
       failure_count = failure_count + 1, 
       last_failure_ts = $2
     -- 原子递增无需应用锁
```

#### 阶段 C: 配置化后端选择

```
修改文件:
  agent_kernel/runtime/kernel_runtime.py

在 KernelRuntimeConfig 中增加:
  persistence_backend: Literal["sqlite", "postgresql"] = "sqlite"
  pg_dsn: str | None = None  # postgresql://user:pass@host:5432/dbname
  pg_pool_min: int = 2
  pg_pool_max: int = 10

在 KernelRuntime.start() 中根据 persistence_backend 选择实例化路径。
```

#### 测试策略

```
新建文件:
  python_tests/agent_kernel/kernel/persistence/test_pg_event_log.py
  python_tests/agent_kernel/kernel/persistence/test_pg_dedupe_store.py
  python_tests/agent_kernel/kernel/persistence/test_pg_circuit_breaker_store.py

方式:
  - 使用 testcontainers-python 启动临时 PostgreSQL
  - 所有现有 SQLite 测试用例需适配为 parametrize(["sqlite", "postgresql"])
  - CI 中 PostgreSQL 测试标记为 @pytest.mark.pg，可选跳过
```

---

### P0-2: 事件 Schema 跨版本迁移框架

**评分影响**: D3.5 (4→5) → 总分 +1

**问题诊断**:
`event_registry.py` 有 `_CURRENT_EVENT_SCHEMA_VERSION = "1"` 和 `validate_event_schema_version()`，
但缺少从 schema v1 到 v2 的运行时迁移路径。当事件格式变更后，旧事件无法被新版本投影服务正确重放。

**改造方案**:

```
新建文件:
  agent_kernel/kernel/persistence/event_schema_migration.py

内容:
  class EventSchemaMigrator:
      """事件 Schema 迁移引擎。
      
      设计原则:
      1. 迁移函数是纯函数: RuntimeEvent → RuntimeEvent
      2. 迁移链可组合: v1→v2→v3 = compose(migrate_v1_to_v2, migrate_v2_to_v3)
      3. 永远不修改原始事件记录（生成新记录，保留 original_schema_version）
      """
      
      _migrations: dict[tuple[str, str], Callable[[RuntimeEvent], RuntimeEvent]]
      
      def register(self, from_version: str, to_version: str, 
                   fn: Callable[[RuntimeEvent], RuntimeEvent]) -> None:
          """注册一个版本迁移函数。"""
      
      def migrate(self, event: RuntimeEvent, target_version: str) -> RuntimeEvent:
          """将事件迁移到目标版本。
          
          如果事件已经是目标版本，原样返回。
          如果没有迁移路径，抛出 SchemaMigrationError。
          """
      
      def migrate_batch(self, events: list[RuntimeEvent], 
                        target_version: str) -> list[RuntimeEvent]:
          """批量迁移。"""
```

**集成点**:
- `DecisionProjectionService` 重放事件前调用 `migrator.migrate(event, current_version)`
- `DispatchOutboxReconciler` 一致性修复时同步迁移
- `EventExportingEventLog` 导出前迁移到最新版本

---

## P1 — 运维信心项

### P1-1: 熔断器 Half-Open 自动探测

**评分影响**: D5.2 (4→5) → 总分 +1

**问题诊断**:
`PlannedRecoveryGateService` 中熔断器有 `half_open_after_ms` 配置，但 half-open 状态的探测
依赖下一次自然请求到达。在流量低谷期，熔断器可能长时间停留在 OPEN 状态，即使下游已恢复。

**改造方案**:

```
新建文件:
  agent_kernel/kernel/recovery/circuit_breaker_probe.py

内容:
  class CircuitBreakerProbeScheduler:
      """定期扫描处于 OPEN 状态且超过 half_open_after_ms 的熔断器，
      发起探测请求以确定是否可以关闭。
      
      设计:
      - 复用 ObservabilityHook.on_circuit_breaker_trip 接收状态变更
      - 探测使用 CompensationRegistry 中已注册的 effect_class 对应的
        轻量级 health check（如果注册了的话）
      - 探测成功: reset failure_count → 熔断器关闭
      - 探测失败: 保持 OPEN，刷新 last_failure_ts，等待下一个周期
      """
      
      def __init__(
          self,
          circuit_breaker_store: CircuitBreakerStore,
          policy: CircuitBreakerPolicy,
          probe_fns: dict[str, Callable[[], Awaitable[bool]]],
          interval_s: float = 60.0,
      ) -> None: ...
      
      def start(self) -> asyncio.Task:
          """启动后台探测循环。"""
      
      async def probe_once(self) -> list[str]:
          """执行一轮探测，返回已关闭的 effect_class 列表。"""
```

**集成点**:
- `KernelRuntime.start()` 中启动探测任务
- `KernelRuntime.stop()` 中 cancel 探测任务

---

### P1-2: 补偿执行超时与重试策略

**评分影响**: D5.3 (4→5) → 总分 +1

**问题诊断**:
`CompensationRegistry` 的 handler 执行没有超时保护。如果补偿操作调用的外部服务不响应，
会导致恢复流程挂起。

**改造方案**:

```
修改文件:
  agent_kernel/kernel/recovery/compensation_registry.py

变更:
  1. CompensationEntry 增加字段:
     timeout_ms: int = 30_000        # 单次补偿超时
     max_attempts: int = 2           # 最大重试次数（含首次）
     backoff_base_ms: int = 1_000    # 退避基数
  
  2. CompensationRegistry.execute() 方法增加:
     - asyncio.wait_for(handler(...), timeout=entry.timeout_ms/1000)
     - 超时抛出 CompensationTimeoutError
     - 重试仅限 TransientCompensationError（新增异常类型）
     - 重试使用 RetryingExecutorService 相同的指数退避+抖动策略
     - 所有尝试耗尽后抛出 CompensationExhaustedError → 
       RecoveryGateService 降级到 human_escalation

新建文件:
  agent_kernel/kernel/recovery/compensation_errors.py
  
内容:
  class CompensationTimeoutError(Exception): ...
  class TransientCompensationError(Exception): ...
  class CompensationExhaustedError(Exception): ...
```

---

### P1-3: Outbox 修复自动调度

**评分影响**: D1.5 (4→5) → 总分 +1

**问题诊断**:
`DispatchOutboxReconciler` 需要手动调用 `reconcile()`。在生产环境中，EventLog 和 DedupeStore
之间的漂移应该被自动检测和修复。

**改造方案**:

```
修改文件:
  agent_kernel/kernel/persistence/dispatch_outbox_reconciler.py

增加:
  class ScheduledOutboxReconciler:
      """定期执行 outbox 一致性修复的调度器。
      
      行为:
      - 每 interval_s 秒执行一次 reconcile()
      - 如果发现 violations，通过 ObservabilityHook 报告
      - 修复结果写入结构化日志
      """
      
      def __init__(
          self,
          reconciler: DispatchOutboxReconciler,
          interval_s: float = 300.0,  # 5分钟
          observability_hook: Any = None,
      ) -> None: ...
      
      def start(self) -> asyncio.Task: ...

集成点:
  - KernelRuntime.start() 中启动
  - KernelRuntime.stop() 中 cancel
  - 健康探针: 暴露 last_reconciliation_result 到 KernelHealthProbe
```

---

### P1-4: KernelRuntime 优雅关停 — Draining 阶段

**评分影响**: D5.7 (4→5) → 总分 +1

**问题诊断**:
`KernelRuntime.stop()` 直接关停各组件，没有等待 in-flight turn 完成的 draining 阶段。
在滚动更新时可能导致正在执行的 turn 被中断。

**改造方案**:

```
修改文件:
  agent_kernel/runtime/kernel_runtime.py

变更 stop() 方法:
  async def stop(self, drain_timeout_s: float = 30.0) -> None:
      """分三阶段关停:
      
      Phase 1 - DRAIN (最多 drain_timeout_s 秒):
        - 停止接受新的 submit_plan / submit_approval 请求
          (KernelFacade 设置 _draining = True，新请求返回 503)
        - 等待所有 in-flight TurnEngine.run_turn() 完成
        - 通过 RunHeartbeatMonitor 追踪活跃 run 数量
      
      Phase 2 - SHUTDOWN:
        - cancel 后台任务 (watchdog, reconciler, probe scheduler)
        - 关停 Temporal Worker (不再 poll 新任务)
      
      Phase 3 - CLEANUP:
        - 关闭持久化连接 (SQLite/PostgreSQL)
        - 执行 WAL checkpoint (SQLite) 或 连接池 close (PG)
      """

新增文件:
  agent_kernel/runtime/drain_coordinator.py
  
内容:
  class DrainCoordinator:
      """追踪 in-flight 操作数量，支持 await drain_complete()。
      
      使用 asyncio.Event 实现:
      - enter(): in_flight_count += 1
      - exit(): in_flight_count -= 1; if 0 then event.set()
      - wait(timeout): await event.wait() with timeout
      """
      
      async def enter(self) -> None: ...
      async def exit(self) -> None: ...
      async def wait(self, timeout_s: float) -> bool: ...
      
      @property
      def in_flight_count(self) -> int: ...
```

---

### P1-5: 健康探针增强 — Startup Probe

**评分影响**: D4.3 (4→5) → 总分 +1

**问题诊断**:
K8s startup probe 用于区分"容器正在初始化"和"容器启动失败"。当前只有 liveness 和 readiness，
Temporal Worker 连接耗时可能导致 liveness 误判。

**改造方案**:

```
修改文件:
  agent_kernel/runtime/health.py

在 KernelHealthProbe 中增加:
  def startup(self) -> dict:
      """Startup probe: 通过当且仅当所有 REQUIRED 级别的 check 报告 OK。
      
      与 readiness 的区别:
      - startup 仅在启动阶段调用，通过后 K8s 切换到 liveness/readiness
      - startup 允许更长的超时 (failureThreshold * periodSeconds)
      - startup 失败 → K8s 重启容器
      """

  def register_check(
      self, name: str, check_fn: HealthCheckFn,
      required_for_startup: bool = False,
  ) -> None:
      """增加 required_for_startup 标记。
      默认 False 保持向后兼容。
      """
```

---

## P2 — 成熟度提升项

### P2-1: EventExportPort Kafka 适配器

**评分影响**: D4.5 (4→5) → 总分 +1

**问题诊断**:
`InMemoryRunTraceStore` 是参考实现，生产环境需要将事件导出到持久化消息队列以供
下游分析系统消费。

**改造方案**:

```
新建文件:
  agent_kernel/kernel/persistence/kafka_event_export.py

内容:
  class KafkaEventExportPort:
      """将 ActionCommit 导出到 Kafka topic。
      
      消息格式:
        key = commit.run_id (保证 per-run 有序)
        value = JSON 序列化的 ActionCommit
        headers = {"schema_version": "1", "event_authority": ...}
      
      配置:
        topic: str = "agent-kernel-events"
        bootstrap_servers: str
        acks: Literal["0", "1", "all"] = "1"
        compression: Literal["none", "gzip", "snappy", "lz4"] = "lz4"
      
      依赖: aiokafka (异步 Kafka 生产者)
      """

替代方案: 
  如果不需要 Kafka，可先实现:
  - FileEventExportPort (JSONL 文件轮转)
  - RedisStreamEventExportPort (Redis Streams)
```

---

### P2-2: 幂等键生成策略标准化

**评分影响**: D1.3 (4→5) → 总分 +1

**问题诊断**:
`IdempotencyEnvelope.dispatch_idempotency_key` 的生成由调用方负责。不同调用方可能
产生不一致的键格式，导致去重失效。

**改造方案**:

```
新建文件:
  agent_kernel/kernel/idempotency_key_policy.py

内容:
  class IdempotencyKeyPolicy:
      """标准化幂等键生成策略。
      
      键格式: {namespace}:{run_id}:{action_id}:{content_hash}
      
      content_hash 计算:
        - 对 Action 的 action_type + params 做 SHA256
        - 与 CapabilitySnapshot.hash 组合
        - 确保: 相同语义请求 → 相同键 → 去重生效
      """
      
      @staticmethod
      def generate(
          run_id: str,
          action: Action,
          snapshot_hash: str,
          namespace: str = "dispatch",
      ) -> str:
          """生成标准幂等键。"""
      
      @staticmethod
      def generate_compensation_key(
          effect_class: str,
          action_id: str,
      ) -> str:
          """生成补偿幂等键 (已有的 f"compensation:{effect_class}:{action_id}" 模式)。"""
```

**集成点**:
- `TurnEngine._build_identity()` 中使用 `IdempotencyKeyPolicy.generate()` 替代手动拼接
- `CompensationRegistry.execute()` 中使用 `.generate_compensation_key()` 替代内联格式

---

### P2-3: SQLite 连接池 (短期方案)

**评分影响**: D2.4 (3→4) → 总分 +1

**问题诊断**:
在 PostgreSQL 适配器就绪之前，SQLite 持久化层仍需要服务。当前单连接 + Lock 的模式
在高并发测试时是瓶颈。

**改造方案**:

```
新建文件:
  agent_kernel/kernel/persistence/sqlite_pool.py

内容:
  class SQLiteConnectionPool:
      """轻量级 SQLite 连接池。
      
      设计:
      - 读连接池: N 个只读连接 (PRAGMA query_only=ON)
      - 写连接: 1 个独占写连接 (WAL 模式下的固有限制)
      - 读写分离: 读操作使用池中连接，写操作获取写连接
      - 连接健康检查: SELECT 1 心跳
      
      这不改变 SQLite 的单写入者限制，但允许并发读取。
      """
      
      def __init__(
          self,
          database_path: str,
          read_pool_size: int = 4,
      ) -> None: ...
      
      def acquire_read(self) -> sqlite3.Connection: ...
      def release_read(self, conn: sqlite3.Connection) -> None: ...
      def acquire_write(self) -> sqlite3.Connection: ...
      def release_write(self, conn: sqlite3.Connection) -> None: ...
      def close_all(self) -> None: ...

作为上下文管理器:
      with pool.read_connection() as conn:
          conn.execute("SELECT ...")
      
      with pool.write_connection() as conn:
          conn.execute("INSERT ...")
```

**集成点**:
- `SQLiteDedupeStore.__init__()` 接受可选 `pool: SQLiteConnectionPool`
- `SQLiteKernelRuntimeEventLog.__init__()` 同理
- 不传 pool 时退化为当前行为（向后兼容）

---

### P2-4: TaskWatchdog 自启动后台循环

**评分影响**: D4.6 内部改善

**问题诊断**:
`TaskWatchdog` 的 `watchdog_once()` 需要外部调度。虽然 `RunHeartbeatMonitor` 已有
`start_watchdog()` 模式，但 `TaskWatchdog` 没有对应实现。

**改造方案**:

```
修改文件:
  agent_kernel/kernel/task_manager/watchdog.py

增加:
  def start(
      self,
      interval_s: float = 60.0,
  ) -> asyncio.Task:
      """启动后台循环，复用 RunHeartbeatMonitor.start_watchdog() 相同模式。"""
      
      async def _loop() -> None:
          try:
              while True:
                  await asyncio.sleep(interval_s)
                  await self.watchdog_once()
          except asyncio.CancelledError:
              pass
      
      return asyncio.get_running_loop().create_task(
          _loop(), name="task_watchdog"
      )
```

---

## 改进实施路线图

```
Week 1-2 (P0):
  ├── P0-1 Phase A: 提取存储端口协议
  ├── P0-1 Phase B: PostgreSQL 适配器实现
  ├── P0-1 Phase C: 配置化后端选择
  └── P0-2: 事件 Schema 迁移框架

Week 3-4 (P1):
  ├── P1-1: 熔断器 Half-Open 自动探测
  ├── P1-2: 补偿执行超时与重试
  ├── P1-3: Outbox 修复自动调度
  ├── P1-4: 优雅关停 Draining 阶段
  └── P1-5: Startup Probe

Week 5-8 (P2):
  ├── P2-1: Kafka EventExportPort
  ├── P2-2: 幂等键生成策略标准化
  ├── P2-3: SQLite 连接池
  └── P2-4: TaskWatchdog 自启动
```

---

## 预期分数提升

| 阶段 | 完成后总分 | 百分比 | 增幅 |
|------|-----------|--------|------|
| 当前 | 142/160 | 88.8% | — |
| P0 完成 | 147/160 | 91.9% | +5 |
| P1 完成 | 152/160 | 95.0% | +5 |
| P2 完成 | 156/160 | 97.5% | +4 |

剩余 4 分差距来自:
- 生产级 PostgreSQL 在真实负载下的验证 (需要 load test)
- Kafka 导出器在高吞吐下的反压策略 (需要实际部署验证)

---

## 新增文件清单

| 文件 | 优先级 | 用途 |
|------|--------|------|
| `kernel/persistence/ports.py` | P0 | 持久化层端口协议 |
| `kernel/persistence/pg_event_log.py` | P0 | PostgreSQL EventLog |
| `kernel/persistence/pg_dedupe_store.py` | P0 | PostgreSQL DedupeStore |
| `kernel/persistence/pg_circuit_breaker_store.py` | P0 | PostgreSQL CircuitBreakerStore |
| `kernel/persistence/pg_recovery_outcome_store.py` | P0 | PostgreSQL RecoveryOutcomeStore |
| `kernel/persistence/pg_colocated_bundle.py` | P0 | PostgreSQL 统一入口 |
| `kernel/persistence/event_schema_migration.py` | P0 | 事件 Schema 迁移引擎 |
| `kernel/recovery/circuit_breaker_probe.py` | P1 | Half-Open 自动探测 |
| `kernel/recovery/compensation_errors.py` | P1 | 补偿错误类型 |
| `runtime/drain_coordinator.py` | P1 | Draining 协调器 |
| `kernel/persistence/kafka_event_export.py` | P2 | Kafka 事件导出 |
| `kernel/idempotency_key_policy.py` | P2 | 幂等键标准化 |
| `kernel/persistence/sqlite_pool.py` | P2 | SQLite 连接池 |

**修改文件清单**:

| 文件 | 优先级 | 变更 |
|------|--------|------|
| `runtime/kernel_runtime.py` | P0+P1 | 后端选择 + draining + 后台任务管理 |
| `kernel/persistence/dispatch_outbox_reconciler.py` | P1 | ScheduledOutboxReconciler |
| `kernel/recovery/compensation_registry.py` | P1 | 超时+重试 |
| `runtime/health.py` | P1 | startup probe |
| `adapters/facade/kernel_facade.py` | P1 | draining 标志 |
| `kernel/turn_engine.py` | P2 | IdempotencyKeyPolicy 集成 |
| `kernel/task_manager/watchdog.py` | P2 | start() 方法 |

---

## 新增依赖

| 包 | 版本 | 优先级 | 用途 |
|----|------|--------|------|
| `asyncpg` | ≥0.29 | P0 | PostgreSQL 异步驱动 |
| `testcontainers[postgresql]` | ≥4.0 | P0 | 测试用临时 PG |
| `aiokafka` | ≥0.10 | P2 | Kafka 异步生产者 |

现有可选依赖无变化: `opentelemetry-api` (已支持)。
