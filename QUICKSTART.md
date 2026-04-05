# QUICKSTART

面向平台接入方的快速上手文档，覆盖：安装、启动、核心接口调用、常见操作。

## 1. 安装

```bash
pip install -e ".[dev]"
```

Temporal 运行需要：

```bash
pip install temporalio
```

## 2. 启动 Runtime

### 2.1 默认（Temporal SDK 模式）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig

config = KernelRuntimeConfig(
    temporal_address="localhost:7233",
    task_queue="agent-kernel",
)

kernel = await KernelRuntime.start(config)
```

### 2.2 LocalFSM 模式（无外部 Temporal）

```python
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.substrate.local.adaptor import LocalSubstrateConfig

config = KernelRuntimeConfig(
    substrate=LocalSubstrateConfig(strict_mode_enabled=False),
)

kernel = await KernelRuntime.start(config)
```

### 2.3 关闭

```python
await kernel.stop()
```

## 3. 运行一个最小流程

```python
from agent_kernel.kernel.contracts import (
    StartRunRequest,
    QueryRunRequest,
    SignalRunRequest,
)

async with await KernelRuntime.start(KernelRuntimeConfig()) as kernel:
    # 1) start
    started = await kernel.facade.start_run(
        StartRunRequest(
            initiator="user",
            run_kind="task",
            input_json={"run_id": "run-quickstart-1"},
        )
    )

    # 2) signal
    await kernel.facade.signal_run(
        SignalRunRequest(
            run_id=started.run_id,
            signal_type="resume_from_snapshot",
            signal_payload={"snapshot_id": "snapshot-1"},
        )
    )

    # 3) query
    view = await kernel.facade.query_run(QueryRunRequest(run_id=started.run_id))
    print(view.lifecycle_state, view.projected_offset)
```

## 4. 常用 Facade 接口

- Run 生命周期
  - `start_run(request)`
  - `signal_run(request)`
  - `cancel_run(request)`
  - `resume_run(request)`
  - `query_run(request)`
  - `query_run_dashboard(run_id)`

- 计划与协作
  - `submit_plan(run_id, plan)`
  - `submit_approval(request)`
  - `commit_speculation(run_id, winner_candidate_id)`

- 子运行
  - `spawn_child_run(request)`

- 健康与能力
  - `get_health()`
  - `get_health_readiness()`
  - `get_manifest()`

- TRACE / Task 管理
  - `query_trace_runtime(run_id)`
  - `open_branch(request)` / `mark_branch_state(request)`
  - `open_stage(...)` / `mark_stage_state(...)`
  - `register_task(descriptor)` / `get_task_status(task_id)` / `list_session_tasks(session_id)`

详细 DTO 见 [INTERFACES.md](./INTERFACES.md)。

## 5. Plan 提交示例

```python
from agent_kernel.kernel.contracts import Action, SequentialPlan

plan = SequentialPlan(
    steps=(
        Action(
            action_id="a1",
            run_id="run-quickstart-1",
            action_type="tool_call",
            effect_class="read_only",
        ),
    )
)

resp = await kernel.facade.submit_plan("run-quickstart-1", plan)
print(resp.accepted, resp.plan_type)
```

## 6. 健康检查示例

```python
print(kernel.facade.get_health())
print(kernel.facade.get_health_readiness())
```

## 7. 质量检查

```bash
python -m ruff check .
python -m pytest -q python_tests/agent_kernel
```
