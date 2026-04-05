# Agent-Kernel 生产级缺陷清单

> 审查视角：大规模工程部署（1000+ 并发 Run、24/7 多进程 Worker、严格幂等要求）
> 审查日期：2026-04-04
> 不包含：已知 PoC 限制、风格问题、文档缺失

---

## 一、CRITICAL — 必须立即修复（会导致运行时崩溃）

### D-C1：Python 2 异常语法 — TurnEngine 快照构建阶段
- **文件**：`agent_kernel/kernel/turn_engine.py:571`
- **类别**：`broken-invariant`
- **根因**：`except CapabilitySnapshotBuildError, ValueError:` 是 Python 2 语法，Python 3 会将 `ValueError` 解析为目标绑定变量而非第二个异常类型，导致该 except 子句语义完全错误——实际上只捕获 `CapabilitySnapshotBuildError` 并把它绑定到名为 `ValueError` 的变量，原来的 `ValueError` 异常会继续往上抛。
- **影响**：快照构建阶段的 `ValueError`（如字段校验失败）不会被捕获，Turn 崩溃而不是进入 `completed_noop` 状态；大规模下任何因数据问题触发 ValueError 的 Turn 都会崩溃。
- **复现**：给 Turn 注入不合法的 snapshot input（触发 ValueError），观察异常穿透。
- **修复方向**：改为 `except (CapabilitySnapshotBuildError, ValueError):`

---

### D-C2：Python 2 异常语法 — TurnEngine 执行方法签名检查
- **文件**：`agent_kernel/kernel/turn_engine.py:1021`
- **类别**：`broken-invariant`
- **根因**：`except TypeError, ValueError:` 同 D-C1，Python 3 下语义错误。
- **影响**：`inspect.signature()` 抛出 `ValueError` 时不会被捕获（`signature = None` 降级路径失效），Turn 崩溃。
- **修复方向**：改为 `except (TypeError, ValueError):`

---

### D-C3：Python 2 异常语法 — RunActorWorkflow 父子生命周期回调
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py:342`
- **类别**：`broken-invariant`, `silent-failure`
- **根因**：`except TemporalError, RuntimeError:` 同 D-C1。
- **影响**：子 Workflow 完成后通知父 Workflow 时，若 Temporal 抛出 `RuntimeError`（如父已关闭），该异常不会被捕获，导致子 Workflow 以异常退出，父端 `active_child_runs` 永远不会清除。
- **修复方向**：改为 `except (TemporalError, RuntimeError):`

---

### D-C4：Python 2 异常语法 — script_runtime 超时处理
- **文件**：`agent_kernel/kernel/cognitive/script_runtime.py:134`
- **类别**：`broken-invariant`
- **根因**：`except TimeoutError, asyncio.CancelledError:` 同 D-C1。
- **影响**：`asyncio.CancelledError` 超时场景下不会被捕获，线程池 executor 无法被及时 shutdown，线程泄漏。
- **修复方向**：改为 `except (TimeoutError, asyncio.CancelledError):`

---

## 二、HIGH — 应在下一迭代修复（会导致数据不一致或并发安全问题）

### D-H1：_seen_signal_tokens 在 append 成功前提前写入 — 信号丢失
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py:369 vs 405`
- **类别**：`error-swallowing`, `broken-invariant`
- **根因**：`_seen_signal_tokens.add(signal_token)` 在第 369 行，但 `append_action_commit` 在第 405 行。如果 append 失败（网络抖动、存储超时），token 已记为"已处理"，下一次重试的信号会在第 367-368 行被直接 return 掉，信号永久丢失。
- **影响**：信号（submit_plan、submit_approval、commit_speculation 等）在存储层失败后，重试无效，Run 状态停滞。大规模下存储抖动必然发生。
- **复现**：Mock `event_log.append_action_commit` 在第一次调用时抛出异常，再次 signal 同一 caused_by，观察信号是否被处理。
- **修复方向**：将 `_seen_signal_tokens.add` 移到 `append_action_commit` 成功之后；或在 except 路径中从 set 中移除 token。

---

### D-H2：_submitted_approvals 无锁并发写 — 批准重复提交
- **文件**：`agent_kernel/adapters/facade/kernel_facade.py:579-581`
- **类别**：`concurrency-safety`, `data-race`
- **根因**：
  ```python
  if dedup_key in self._submitted_approvals:  # 读
      return
  self._submitted_approvals.add(dedup_key)    # 写
  ```
  两行之间没有原子保护。多线程并发（如 FastAPI worker pool）同时提交同一 approval 时，两个线程都能通过 `in` 检查后同时 add，导致 signal 被发两次。
- **影响**：审批重复触发，Workflow 收到两个 approval_submitted 信号（despite caused_by dedupe，两次 facade 调用的 caused_by 可能不同）。
- **修复方向**：用 `threading.Lock` 保护 check-then-add，或改用 `asyncio.Lock`（如 Facade 在单事件循环内使用）。

---

### D-H3：continue_as_new 丢失 _pending_signals
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py:444-448`
- **类别**：`silent-failure`, `incorrect-contract`
- **根因**：`_trigger_continue_as_new()` 只携带 `run_id/session_id/parent_run_id`，不携带 `_pending_signals`：
  ```python
  temporal_workflow.continue_as_new(
      RunInput(run_id=self._run_id, session_id=self._session_id, parent_run_id=self._parent_run_id)
  )
  ```
  Temporal 在触发 continue_as_new 后可能仍有 buffered signals 等待投递到旧实例，旧实例已结束，新实例 `_pending_signals` 为空。
- **影响**：恰好在 History 阈值处到达的信号被丢弃，Run 状态停滞（无法收到 approval、plan 等关键信号）。
- **修复方向**：在 continue_as_new 前确保所有 pending_signals 已被 flush 到 event_log；或扩展 `RunInput` 携带 pending_signals 序列化列表。

---

### D-H4：_active_speculative_tasks 嵌套覆盖 — 外层 Task 泄漏
- **文件**：`agent_kernel/kernel/plan_executor.py:564`
- **类别**：`resource-leak`, `broken-invariant`
- **根因**：`self._active_speculative_tasks = tasks` 是直接赋值。若 SpeculativePlan 的 candidate 本身也是 SpeculativePlan（嵌套投机），内层执行会覆盖外层的 dict，外层 Task 引用丢失，永远无法通过 `commit_speculation` 取消。
- **影响**：外层候选 Task 在后台持续运行，消耗资源，可能产生意外的副作用（对 ExecutorService 的调用）。
- **修复方向**：改用栈结构（`list[dict]`）保存/恢复，或禁止嵌套 SpeculativePlan（在 `_execute_speculative` 入口处断言 candidate.plan 不是 SpeculativePlan）。

---

### D-H5：事件日志 append 失败无回滚 — Turn 结果丢失
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py:648, 676`
- **类别**：`error-swallowing`, `broken-invariant`
- **根因**：`_append_turn_outcome_commit`（648）和 `_append_recovery_event`（676）在 `append_action_commit` 失败时异常向上传播，但没有任何回滚逻辑：
  - Dedupe store 已将 key 标记为 dispatched
  - TurnEngine 已执行 executor（副作用已产生）
  - Event log 没有记录
  - Replay 时会重新执行该 Turn，造成重复副作用
- **影响**：存储层一次瞬时失败导致动作被执行两次（幂等性被破坏）。
- **修复方向**：在 append 失败时，将 dedupe key 状态回退为 `reserved`（或 rollback），或使用事务性写入模式。

---

### D-H6：KernelFacade.get_health 不支持 async 探针 — K8s 健康检查静默错误
- **文件**：`agent_kernel/adapters/facade/kernel_facade.py`（get_health 方法）
- **类别**：`missing-boundary-validation`, `incorrect-contract`
- **根因**：`get_health()` 同步调用 `health_probe.liveness()`，若该方法是 `async def`，则返回 coroutine 对象而非实际 dict，K8s 收到不可序列化的响应。
- **影响**：所有使用 async health probe 的部署，K8s liveness 检查永久失败，触发不必要的 Pod 重启。
- **修复方向**：在调用前检查 `asyncio.iscoroutinefunction(probe_result)`，若是则拒绝注入（或要求调用方通过 `await` 包装器提供）；或将 `get_health` 改为 `async`。

---

## 三、MEDIUM — 应在近期迭代排期（影响长期稳定性）

### D-M1：InMemoryRuntimeEventLog 无界增长 — OOM 风险
- **文件**：`agent_kernel/kernel/minimal_runtime.py`（`_events_by_run` dict）
- **类别**：`memory-leak`, `resource-leak`
- **根因**：`_events_by_run: dict[str, list[RuntimeEvent]]` 只追加，从不清理。1000 并发 Run × 10,000 事件 = 1000 万个 RuntimeEvent 对象常驻内存。
- **影响**：长时间运行的 Worker 触发 OOM，进程被杀，所有 in-flight Run 中断。
- **修复方向**：对已完成 Run 实施 TTL 清理策略；或为 InMemory 实现添加最大 Run 数量上限。

---

### D-M2：InMemoryDecisionProjectionService 无锁并发投影 — 状态覆盖
- **文件**：`agent_kernel/kernel/minimal_runtime.py`（`_projection_by_run` dict）
- **类别**：`data-race`, `concurrency-safety`
- **根因**：`catch_up()` 和 `get()` 都读写 `_projection_by_run` 字典，asyncio 协程切换时（在 `await` 点）两个协程可能同时操作同一 run_id 的投影，后写覆盖前写。
- **影响**：竞态导致投影状态回退，DecisionProjectionService 返回历史快照，造成重复决策。
- **修复方向**：per-run_id asyncio.Lock；或在 asyncio 单线程内保证串行（但需文档限制）。

---

### D-M3：DedupeStore 状态机 reserved→dispatched 非原子 — 重复 dispatch
- **文件**：`agent_kernel/kernel/turn_engine.py:663-704`
- **类别**：`concurrency-safety`, `missing-idempotency`
- **根因**：`reserve()` 调用（663）和 `mark_dispatched()` 调用（704）之间存在约 40 行可被 await 打断的代码。若同一 dedupe_key 在此窗口内被第二个协程尝试 reserve，它会看到 `reserved` 状态并等待；但若第一个协程因异常跳过 `mark_dispatched`，第二个协程永远等待，或在超时后认为可以继续。
- **影响**：在高并发 Replay 或 Retry 场景下，相同动作被 dispatch 两次。
- **修复方向**：用 `async with lock` 包裹 reserve→dispatch 整个原子区间；或在 reserve 成功后立即在同一 await 点 mark_dispatched。

---

### D-M4：CapabilitySnapshotBuilder 接受可变列表输入 — 哈希漂移
- **文件**：`agent_kernel/kernel/capability_snapshot.py`
- **类别**：`data-race`, `broken-invariant`
- **根因**：`build(input_value)` 对 `input_value.tool_bindings` 等字段做 sort+dedupe，但不对 `input_value` 做防御性拷贝。若调用方复用同一 `CapabilitySnapshotInput` 对象并在两次 build 调用之间修改列表字段，两次生成的 hash 不同，但代码层面看起来是"同一输入"。
- **影响**：approval gate 基于 snapshot hash 做比较，hash 漂移导致已批准的快照被拒绝，或反之（hash 碰巧相同但语义不同）。
- **修复方向**：在 `build()` 入口处 deepcopy input_value，或将 `CapabilitySnapshotInput` 字段全部改为 `frozenset`/`tuple`（Python 强制不可变）。

---

### D-M5：_timed_out 集合脏留存 — Run 超时重复触发或沉默
- **文件**：`agent_kernel/runtime/heartbeat.py`
- **类别**：`resource-leak`, `silent-failure`
- **根因**：`watchdog_once()` 将超时 run 加入 `_timed_out` set 后发送 signal；若 signal delivery 失败（Temporal 不可达），Run 仍停滞，但该 run_id 已在 `_timed_out` 中，后续循环不会再重试 signal。
- **影响**：网络抖动导致超时信号丢失后，Run 永久停滞（watchdog 认为已处理）。
- **修复方向**：只有在 signal delivery 确认成功后才将 run_id 加入 `_timed_out`；或改用带 TTL 的 set，超时后自动清除以允许重试。

---

### D-M6：plan_executor 字段未在 _handle_signal 中 None 检查
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py`（`_handle_signal` 中 speculation_committed 分支）
- **类别**：`missing-boundary-validation`, `incorrect-type`
- **根因**：`RunActorDependencyBundle.plan_executor` 是 `Any | None = None`（可选字段），但 `_handle_signal` 处理 `speculation_committed` 信号时若忘记 `if self._plan_executor is not None` 检查，会触发 `AttributeError: 'NoneType' object has no attribute 'commit_speculation'`。
- **影响**：未配置 PlanExecutor 的 Worker 收到投机提交信号时崩溃，整个 Workflow 进入 failed 状态。
- **修复方向**：当前代码尚未实现信号分支，实现时必须加 None guard；或将 plan_executor 改为必填字段并在 Worker 启动时 fail-fast 校验。

---

### D-M7：Worker 优雅关闭未等待 in-flight Workflow 任务完成
- **文件**：`agent_kernel/substrate/temporal/worker.py`
- **类别**：`resource-leak`, `broken-invariant`
- **根因**：SIGTERM 处理器调用 `worker.shutdown()` 后立即返回，Temporal SDK worker 的 shutdown 可能在 in-flight workflow task 执行期间中断，导致 Turn 状态写入一半即停止。
- **影响**：滚动部署时，正在执行的 Turn 被强制中断，事件日志残缺，下次 Replay 行为不确定。
- **修复方向**：在 shutdown 后 await 所有活跃 asyncio Task 完成（带超时），确保 graceful drain；或依赖 Temporal SDK 的 worker.run() 的内置 drain 语义（需确认版本）。

---

## 四、LOW — 技术债（不影响当前正确性，但会增加维护成本）

### D-L1：dedupe 降级时无可观测信号
- **文件**：`agent_kernel/kernel/turn_engine.py:1034-1069`（`_reserve_with_degradation`）
- **类别**：`silent-failure`
- **根因**：Dedupe Store 不可用时，非幂等动作（`compensatable_write`、`fire_forget`）会直接硬失败，而幂等动作会静默降级继续执行，没有结构化日志也没有 Metric 记录。
- **影响**：运维无法发现 dedupe 服务不可用，系统悄悄降级运行，风险不可见。
- **修复方向**：在降级路径加 structured log（level=WARNING）并通过 observability_hook 发射 `dedupe_degraded` 诊断事件。

---

### D-L2：process_action_commit 中断言在结果写入前执行 — 状态不一致
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py`（process_action_commit）
- **类别**：`broken-invariant`
- **根因**：`_assert_single_dispatch_attempt_in_turn(turn_result)` 在 `_append_turn_outcome_commit` 之前执行。断言失败时 TurnEngine 已完成，但事件日志中无该 Turn 的记录，Recovery Gate 被触发但没有 Turn Outcome 作为基础。
- **影响**：不符合"先提交事实，再断言不变量"的 event sourcing 原则；边界情况下 Recovery 行为不确定。
- **修复方向**：先 append outcome，再断言；或将断言前置到 TurnEngine 内部，确保 Turn 不能返回非法状态。

---

### D-L3：Any 类型掩盖核心协议合约
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py:130-135`（RunActorDependencyBundle）
- **类别**：`incorrect-type`
- **根因**：`context_port`, `llm_gateway`, `output_parser`, `reflection_policy`, `reflection_builder`, `plan_executor` 均为 `Any | None`，既无运行时校验，也无静态类型检查覆盖。
- **影响**：注入错误类型时只在深层调用时报 AttributeError，而非启动时 fail-fast；mypy/pyright 无法检查注入合规性。
- **修复方向**：逐步将 Any 替换为对应 Protocol 类型（或至少 `object` + `assert isinstance`）；或在 `configure_run_actor_dependencies` 中加运行时类型断言。

---

## 缺陷统计（第一轮）

| 级别     | 数量 | 必须修复条件 | 状态 |
|----------|------|-------------|------|
| CRITICAL | 4    | 上线前全部修复 | ✅ 全部已修复 |
| HIGH     | 6    | 下一迭代全部修复 | ✅ 全部已修复 |
| MEDIUM   | 7    | 近 2 个迭代内修复 | ✅ 全部已修复 |
| LOW      | 3    | 作为技术债持续改善 | ✅ 全部已修复 |
| **合计** | **20** | | ✅ **20/20 已修复** |

---

## 修复清单（第一轮）

```
D-C1 ~ D-C4   Python 2 异常语法                    ✅ 已修复
D-H1           seen_signal_tokens 位置调整           ✅ 已修复（append 成功后才 add）
D-H2           submitted_approvals 加锁              ✅ 已修复（threading.Lock + with 块）
D-H3           continue_as_new 信号安全              ✅ 已修复（RunInput.pending_signals）
D-H4           speculative tasks 嵌套覆盖            ✅ 已修复（_speculative_task_stack）
D-H5           append 失败无回滚                     ✅ 已修复（mark_unknown_effect on append fail）
D-H6           async 健康探针兼容                    ✅ 已修复（TypeError 快速失败）
D-M1           InMemoryEventLog 无界增长             ✅ 已修复（_MAX_RETAINED_RUNS + cleanup_completed_run）
D-M2           InMemory 投影无锁并发                 ✅ 已修复（per-run asyncio.Lock）
D-M3           DedupeStore reserve→dispatch 非原子   ✅ 已修复（reserve_and_dispatch() 原子方法）
D-M4           CapabilitySnapshot 可变输入           ✅ 已修复（build() 入口 deepcopy）
D-M5           _timed_out 脏留存                     ✅ 已修复（signal 失败后 discard）
D-M6           plan_executor None guard 缺失         ✅ 已修复
D-M7           Worker 优雅关闭                       ✅ 已修复（shutdown() + drain）
D-L1           dedupe 降级无可观测信号               ✅ 已修复（dedupe_degraded 事件 + warning log）
D-L2           断言在 append 之前                    ✅ 已修复（assert 移至 append 成功后）
D-L3           configure_run_actor_dependencies 缺 fail-fast  ✅ 已修复
```

---

## 第二轮补充缺陷（2026-04-05 生产工程规模审查）

> 审查范围：TaskManager 新模块 + SQLite 持久化层 + 安全扫描（ruff S 规则）
> 新增缺陷总数：7

---

### D-C5：SQLiteDeDupeStore 缺少 check_same_thread=False — 多线程崩溃
- **文件**：`agent_kernel/kernel/persistence/sqlite_dedupe_store.py:35`
- **类别**：`concurrency-safety`, `broken-invariant`
- **根因**：`sqlite3.connect(self._database_path, isolation_level=None)` 未设置 `check_same_thread=False`。SQLite 默认拒绝跨线程连接访问，Temporal Worker 在 thread-pool 中调用 DedupeStore 时触发 `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`，导致 Turn 中断。
- **影响**：所有使用 SQLite DedupeStore 的生产部署，在任何多线程场景下（asyncio + thread executor、FastAPI worker pool）必然崩溃。
- **优先级**：P0（上线前修复）
- **修复方向**：`sqlite3.connect(self._database_path, isolation_level=None, check_same_thread=False)` 并加 `threading.Lock` 保护写操作。

---

### D-C6：SQLiteTurnIntentLog 缺少线程安全三件套 — 数据损坏
- **文件**：`agent_kernel/kernel/persistence/sqlite_turn_intent_log.py:20`
- **类别**：`concurrency-safety`, `data-race`
- **根因**：`sqlite3.connect` 缺少 `check_same_thread=False`、无 `threading.Lock` 保护、未启用 WAL 模式（`PRAGMA journal_mode=WAL`），而同类的 `SQLiteEventLog` 已正确设置 WAL。
- **影响**：多线程写入时 WAL 缺失导致 database locked 错误；并发读写可能产生事务冲突，TurnIntent 记录损坏；多个 Worker 同时写时概率性崩溃。
- **优先级**：P0（上线前修复）
- **修复方向**：添加 `check_same_thread=False` + `threading.Lock` + `PRAGMA journal_mode=WAL`，与 SQLiteEventLog 保持一致。

---

### D-H7：TaskWatchdog 绕过锁直接访问 TaskRegistry 私有字段
- **文件**：`agent_kernel/kernel/task_manager/watchdog.py:110`
- **类别**：`concurrency-safety`, `incorrect-type`
- **根因**：
  ```python
  task = self._registry._run_index.get(run_id)  # 直接访问私有字段，绕过锁
  ```
  `TaskRegistry._run_index` 由 `threading.Lock` 保护，但 Watchdog 直接读取绕过锁，在高并发下产生数据竞争。
- **影响**：Watchdog 读取到部分写入的 task 对象，基于脏数据做心跳判断，可能误触发 stall 检测或漏检。
- **优先级**：P1（下一迭代）
- **修复方向**：在 `TaskRegistry` 暴露 `get_by_run_id(run_id)` 公开方法（持有锁读取），Watchdog 调用该方法。

---

### D-H8：backoff_base_ms 字段声明但 _launch_retry 从不使用 — 重试风暴
- **文件**：`agent_kernel/kernel/task_manager/restart_policy.py:220`（`_launch_retry`）
- **类别**：`incorrect-contract`, `missing-boundary-validation`
- **根因**：`TaskRestartPolicy.backoff_base_ms` 字段存在于合约中，表示基础退避时间，但 `_launch_retry()` 不包含任何 `asyncio.sleep()` 调用，重试立即触发，相当于无退避。
- **影响**：任务连续失败时，Worker 立即发起下一次 Run，对 Temporal Server 和下游服务产生突发请求压力（重试风暴）；在分布式场景下雪崩效应显著。
- **优先级**：P1（下一迭代）
- **修复方向**：在 `_launch_retry()` 中计算 `sleep_ms = backoff_base_ms * (2 ** (next_seq - 1))`（指数退避），在发起 `start_run` 前执行 `await asyncio.sleep(sleep_ms / 1000.0)`；加 max_backoff_ms 上限。

---

### D-H9：TaskWatchdog 直接调用 complete_attempt 绕过 RestartPolicyEngine
- **文件**：`agent_kernel/kernel/task_manager/watchdog.py:108-113`
- **类别**：`broken-invariant`, `incorrect-contract`
- **根因**：Watchdog 检测到 stall 后直接调用 `self._registry.complete_attempt(run_id, "failed")` 和 `self._registry.update_state(task_id, "stalled")`，绕过 `RestartPolicyEngine.handle_failure()`，导致：
  - restart_policy 的 max_attempts 预算被绕过（不扣除次数）
  - on_exhausted 策略（escalate/reflect/abort）被忽略
  - ReflectionOrchestrator 不会被触发
  - TaskEventLog 不会收到标准的失败事件序列
- **影响**：stall 场景下任务直接死亡，不走反思和重启逻辑，任务管理模块的核心价值被架空。
- **优先级**：P1（下一迭代）
- **修复方向**：Watchdog 检测到 stall 后调用 `await restart_engine.handle_failure(task_id, run_id, failure=stall_failure_envelope)`，由 RestartPolicyEngine 统一决策。

---

### D-M8：安全扫描 — exec/SQL 注入/不安全随机数三项违规
- **类别**：`security-vulnerability`
- **根因**（ruff S 规则扫描结果）：
  1. **S102 (exec)**：代码中存在 `exec()` 调用，可能接受外部输入，远程代码执行风险
  2. **S608 (SQL f-string)**：SQL 语句通过 f-string 拼接，未使用参数化查询，SQL 注入风险
  3. **S311 (random.randint)**：使用 `random` 模块生成值，若用于任何安全相关场景（如 token、nonce）则不安全
- **影响**：S608 是可直接利用的 SQL 注入漏洞；S102 在多租户场景下是 RCE 向量；S311 在安全场景下降低熵。
- **优先级**：P1（安全规范要求）
- **修复方向**：S608 改用参数化查询 `cursor.execute(sql, params)`；S311 改用 `secrets` 模块或 `os.urandom`（非安全场景保留 random 并加 `# noqa: S311` 说明）；S102 review exec 调用确认输入来源，必要时加白名单校验。

---

### D-L4：peer_auth 模块未集成到 RunActorWorkflow 信号路由
- **文件**：`agent_kernel/substrate/temporal/run_actor_workflow.py`（`_handle_signal` 方法）
- **类别**：`missing-boundary-validation`, `incomplete-implementation`
- **根因**：`peer_auth.is_peer_run_authorized()` 已实现完整的双层授权逻辑（production tier 和 PoC fallback），但 `RunActorWorkflow._handle_signal()` 在处理来自 peer 的信号时未调用该函数，任何知道 Workflow ID 的调用方都可以发送信号。
- **影响**：在生产多租户场景下，非授权 Run 可以向任意 Run 发送 `submit_plan`、`commit_speculation` 等信号，绕过 peer 授权边界。
- **优先级**：P2（投产前补充）
- **修复方向**：在 `_handle_signal` 的 peer-origin 分支中添加：
  ```python
  if not is_peer_run_authorized(signal.from_peer_run_id, self._snapshot, self._active_child_runs):
      logger.warning("unauthorized peer signal rejected ...")
      return
  ```

---

## 缺陷统计（含第二轮）— 最终状态

| 级别     | 第一轮 | 第二轮补充 | 总计 | 已修复 |
|----------|--------|-----------|------|--------|
| CRITICAL | 4      | 2 (D-C5,C6) | 6  | ✅ 6/6 |
| HIGH     | 6      | 3 (D-H7~H9) | 9  | ✅ 9/9 |
| MEDIUM   | 7      | 1 (D-M8)   | 8  | ✅ 8/8 |
| LOW      | 3      | 1 (D-L4)   | 4  | ✅ 4/4 |
| **合计** | **20** | **7**     | **27** | ✅ **27/27 全部已修复** |

## 修复清单（第二轮）

```
D-C5   sqlite_dedupe_store.py       check_same_thread=False + threading.Lock  ✅
D-C6   sqlite_turn_intent_log.py    check_same_thread=False + threading.Lock + WAL  ✅
D-H7   watchdog.py                  get_task_id_for_run() 公开方法  ✅
D-H8   restart_policy.py            指数退避 asyncio.sleep  ✅
D-H9   watchdog.py                  改为调用 RestartPolicyEngine.handle_failure  ✅
D-M8   script_runtime/consistency   移除 noqa S102/S311，参数化表名引用  ✅
D-L4   run_actor_workflow.py        is_peer_run_authorized() 集成到 _handle_signal  ✅
```

## 第三轮额外优化（2026-04-05）

```
D-M3 (原)   reserve_and_dispatch() 原子方法（消除 reserve→dispatch 窗口）  ✅
D-M1 (原)   InMemoryEventLog cleanup_completed_run() + _MAX_RETAINED_RUNS  ✅
D-M4 (原)   CapabilitySnapshotBuilder.build() deepcopy 防御性拷贝  ✅
D-L3 (原)   configure_run_actor_dependencies() fail-fast 必填字段校验  ✅
D-H5 (原)   _append_turn_outcome_commit 失败时 mark_unknown_effect 回滚  ✅
TRACE-review_state  query_trace_runtime() review_state 从 human gate 状态推导  ✅
```

## PoC 已知限制（不计入缺陷，标记为架构演进项）

```
P-1  SpeculativePlan 无 Temporal Child Workflow 跨进程隔离（需 Child Workflow API）
P-2  KernelFacade 无 per-caller 认证（需网关层 JWT/mTLS）
P-3  TraceRuntimeView branch/stage 来自 facade 内存注册表（V2：事件重建）
```

## ⚠️ 原始 P0/P1 批次（已过期，均已修复）

```
DC-05  sqlite_dedupe_store.py:35   check_same_thread=False + threading.Lock
DC-06  sqlite_turn_intent_log.py   check_same_thread=False + threading.Lock + WAL
DH-09  watchdog.py:108             改为调用 RestartPolicyEngine.handle_failure
DM-08  安全扫描                    参数化查询 + secrets 模块
```
