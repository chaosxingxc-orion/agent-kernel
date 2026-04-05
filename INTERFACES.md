# INTERFACES

本文件记录当前 `agent-kernel` 的主要对外接口（以代码实现为准）。

## 1. 主入口

### 1.1 KernelRuntime

路径：`agent_kernel/runtime/kernel_runtime.py`

- `KernelRuntime.start(config, temporal_client=None)`
- `runtime.stop()`
- `runtime.facade`
- `runtime.gateway`
- `runtime.health`

### 1.2 KernelFacade

路径：`agent_kernel/adapters/facade/kernel_facade.py`

#### Run API
- `start_run(StartRunRequest) -> StartRunResponse`
- `signal_run(SignalRunRequest) -> None`
- `cancel_run(CancelRunRequest) -> None`
- `resume_run(ResumeRunRequest) -> None`
- `query_run(QueryRunRequest) -> QueryRunResponse`
- `query_run_dashboard(run_id) -> QueryRunDashboardResponse`
- `stream_run_events(run_id, include_derived_diagnostic=False)`

#### Plan / Approval API
- `submit_plan(run_id, plan) -> PlanSubmissionResponse`
- `submit_approval(ApprovalRequest) -> None`
- `commit_speculation(run_id, winner_candidate_id) -> None`

#### Child Run API
- `spawn_child_run(SpawnChildRunRequest) -> SpawnChildRunResponse`

#### Health / Manifest API
- `get_manifest() -> KernelManifest`
- `get_health() -> dict`
- `get_health_readiness() -> dict`

#### Task / Trace API
- `register_task(TaskDescriptor) -> None`
- `get_task_status(task_id) -> TaskHealthStatus | None`
- `list_session_tasks(session_id) -> list[TaskDescriptor]`
- `query_trace_runtime(run_id) -> TraceRuntimeView`
- `record_task_view(TaskViewRecord) -> str`
- `get_task_view_record(task_view_id) -> TaskViewRecord | None`
- `get_task_view_by_decision(run_id, decision_ref) -> TaskViewRecord | None`
- `bind_task_view_to_decision(task_view_id, decision_ref) -> None`
- `open_stage(stage_id, run_id, branch_id=None) -> None`
- `mark_stage_state(run_id, stage_id, new_state, failure_code=None) -> None`
- `open_branch(OpenBranchRequest) -> None`
- `mark_branch_state(BranchStateUpdateRequest) -> None`
- `open_human_gate(HumanGateRequest) -> None`
- `get_action_state(dispatch_idempotency_key) -> str | None`

## 2. 核心 DTO

路径：`agent_kernel/kernel/contracts.py`

### 2.1 Run DTO
- `StartRunRequest`
- `StartRunResponse`
- `SignalRunRequest`
- `CancelRunRequest`
- `ResumeRunRequest`
- `QueryRunRequest`
- `QueryRunResponse`
- `QueryRunDashboardResponse`
- `SpawnChildRunRequest`
- `SpawnChildRunResponse`

### 2.2 Plan DTO
- `SequentialPlan`
- `ParallelPlan`
- `ConditionalPlan`
- `DependencyGraph`
- `SpeculativePlan`

### 2.3 Facade 协作 DTO
- `ApprovalRequest`
- `PlanSubmissionResponse`
- `KernelManifest`

### 2.4 Trace / Task DTO
- `TraceRuntimeView`
- `TraceBranchView`
- `TraceStageView`
- `TaskViewRecord`
- `OpenBranchRequest`
- `BranchStateUpdateRequest`
- `HumanGateRequest`

## 3. Substrate 配置接口

### 3.1 TemporalSubstrateConfig
路径：`agent_kernel/substrate/temporal/adaptor.py`

关键字段：
- `mode`: `"sdk" | "host"`
- `address`
- `namespace`
- `task_queue`
- `workflow_id_prefix`
- `strict_mode_enabled`
- `host_port` / `host_db_filename` / `host_ui_port`

### 3.2 LocalSubstrateConfig
路径：`agent_kernel/substrate/local/adaptor.py`

关键字段：
- `workflow_id_prefix`
- `strict_mode_enabled`

## 4. 示例：Facade 最小调用序列

```python
started = await kernel.facade.start_run(StartRunRequest(initiator="user", run_kind="task"))
await kernel.facade.signal_run(SignalRunRequest(run_id=started.run_id, signal_type="cancel_requested"))
view = await kernel.facade.query_run(QueryRunRequest(run_id=started.run_id))
```

## 5. 兼容与约束

- 平台侧不应直接调用 substrate 实现细节，统一通过 `KernelFacade`
- DTO 约定为不可变（frozen dataclass）
- `get_manifest()` 用于能力发现，建议平台启动时缓存
