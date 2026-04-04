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

## 缺陷统计

| 级别     | 数量 | 必须修复条件 |
|----------|------|-------------|
| CRITICAL | 4    | 上线前全部修复 |
| HIGH     | 6    | 下一迭代全部修复 |
| MEDIUM   | 7    | 近 2 个迭代内修复 |
| LOW      | 3    | 作为技术债持续改善 |
| **合计** | **20** | |

---

## 快速修复优先级

```
1. D-C1 ~ D-C4   Python 2 语法（单行改动，无风险）
2. D-H1           seen_signal_tokens 位置调整（信号丢失最严重场景）
3. D-H2           submitted_approvals 加锁（并发安全）
4. D-H3           continue_as_new 信号安全（数据完整性）
5. D-H4           speculative tasks 嵌套覆盖（资源泄漏）
6. D-H5           append 失败无回滚（幂等性）
7. D-H6           async 健康探针兼容（运维安全）
```
