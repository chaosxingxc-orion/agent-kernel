# HOST_ACTIVATION_GUIDE.md

> 版本：V6.4 | 适用对象：平台接入工程师、内核集成团队
> 本文档说明 `SandboxGrant.host_kind` 七种执行宿主的激活方式、安全边界与配置参数。

---

## 宿主种类总览

内核通过 `SandboxGrant.host_kind` 字段声明动作的执行宿主。Admission 决定是否发放 `SandboxGrant`；Executor 依据 `host_kind` 路由执行。

| host_kind | 执行位置 | 默认网络策略 | 适用场景 |
|---|---|---|---|
| `local_process` | 本地子进程 | `deny_all` | 受控本地命令行工具 |
| `local_cli` | 本地 CLI 调用 | `deny_all` | 结构化 CLI 工具（flags 已知） |
| `cli_process` | 本地 CLI（通用进程） | `deny_all` | 兼容旧版 CLI 调用路径 |
| `in_process_python` | 当前 Python 进程内 | N/A（无网络隔离） | 内部 Python 函数注册表 |
| `remote_service` | 远程 HTTP/gRPC 服务 | `allow_list` | 外部 API、微服务调用 |
| `llm_inference` | 远程模型推理端点 | `allow_list` | LLM API（OpenAI、Anthropic 等） |
| `remote_mcp_server` | 远程 MCP 协议服务器 | `allow_list` | MCP tool 调用 |

---

## 1. `local_process`

**语义**：以子进程方式执行可执行文件，标准 I/O 传递输入输出。

**SandboxGrant 配置示例**：

```python
from agent_kernel.kernel.contracts import SandboxGrant

grant = SandboxGrant(
    grant_ref="grant:local-proc-001",
    host_kind="local_process",
    sandbox_profile_ref="profile:subprocess-v1",
    allowed_mounts=["/tmp/workspace"],
    denied_mounts=["/etc", "/root"],
    network_policy="deny_all",
)
```

**安全边界**：
- 进程不得访问内核内存；使用 `allowed_mounts` 限制文件系统访问范围。
- `denied_mounts` 优先级高于 `allowed_mounts`。
- 子进程超时由 `Action.timeout_ms` 控制；超时后 Executor 应终止进程并记录 `FailureEnvelope`。
- 不应在此 host 挂载包含凭证的目录（如 `~/.aws`、`~/.ssh`）。

**Executor 实现要求**：
- 实现 `ExecutorService.execute(action, grant_ref)` — 从 `action.input_json` 解析命令参数。
- 超时后通过 `FailureEnvelope(failed_stage="execution", failure_class="transient")` 汇报 Recovery。

---

## 2. `local_cli`

**语义**：调用本地已知 CLI 工具，参数结构化（flags/subcommand 已声明）。

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:local-cli-git",
    host_kind="local_cli",
    allowed_mounts=["/tmp/repo"],
    denied_mounts=["/etc"],
    network_policy="deny_all",
)
```

**与 `local_process` 的区别**：
- `local_cli` 预期调用方已知 CLI 接口（如 `git`、`ruff`、`kubectl`），参数已做 schema 校验。
- `local_process` 适合通用命令行，参数由 `action.input_json["argv"]` 传入，schema 更宽松。
- 两者网络策略均默认 `deny_all`；CLI 工具不应发起出站连接。

---

## 3. `cli_process`

**语义**：兼容旧版 CLI 路径，等价于通用本地进程调用。新接入建议优先使用 `local_process` 或 `local_cli`。

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:cli-proc-compat",
    host_kind="cli_process",
    network_policy="deny_all",
)
```

**注意**：如无历史兼容需求，新团队应避免使用此 host_kind。

---

## 4. `in_process_python`

**语义**：在当前 Python 进程内直接调用已注册的异步函数，无子进程开销，无网络隔离。

**适用场景**：纯 Python 内部逻辑（计算、转换、内存操作），不涉及外部 I/O 或副作用。

**注册方式**：

```python
from agent_kernel.kernel.minimal_runtime import InProcessPythonExecutorService
from agent_kernel.kernel.contracts import Action

executor = InProcessPythonExecutorService()

async def handle_summarize(action: Action, grant_ref: str | None) -> dict:
    text = (action.input_json or {}).get("text", "")
    return {"summary": text[:100]}

executor.register("summarize", handle_summarize)
```

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:in-proc-py",
    host_kind="in_process_python",
    # network_policy 不适用：in-process 执行无网络边界
)
```

**安全边界**：
- 无网络策略隔离：注册函数有权访问进程内全部状态，须由代码审查保证安全性。
- `action_type` 必须在 `InProcessPythonExecutorService._registry` 中注册，否则抛出 `KeyError`。
- 不应注册包含外部 I/O（HTTP、数据库）的函数至此 host；应使用 `remote_service`。

**错误处理**：
- 未注册的 `action_type` → `KeyError`，内核记录为 `FailureEnvelope(failure_class="deterministic")`。
- 注册函数抛出异常 → 内核捕获并包装为 `FailureEnvelope(failure_class="unknown")`。

---

## 5. `remote_service`

**语义**：通过 HTTP/gRPC 调用远程微服务或第三方 API，网络出站受 `allow_list` 管控。

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:remote-svc-billing",
    host_kind="remote_service",
    network_policy="allow_list",
    allowed_hosts=["billing.internal:443", "api.stripe.com:443"],
)
```

**幂等性合约**：使用 `RemoteServiceIdempotencyContract` 声明远程服务的幂等性能力：

```python
from agent_kernel.kernel.contracts import RemoteServiceIdempotencyContract

contract = RemoteServiceIdempotencyContract(
    accepts_dispatch_idempotency_key=True,
    returns_stable_ack=True,
    peer_retry_model="exactly_once_claimed",
    default_retry_policy="bounded_retry",
)
```

**安全边界**：
- `allowed_hosts` 须精确声明每个出站主机（含端口），禁止通配符。
- `network_policy="deny_all"` 将阻止所有出站连接；确认在 `SandboxGrant` 中设为 `allow_list`。
- 凭证（API key、token）不应出现在 `action.input_json` 中；应通过 Executor 层环境变量或 vault 注入。

**幂等性要求**：
- `ExternalIdempotencyLevel="unknown"` 时，内核 Executor 不保证幂等性；需 `human_escalation` recovery 模式。
- `ExternalIdempotencyLevel="guaranteed"` 时，内核可安全重试；`default_retry_policy="bounded_retry"`。

---

## 6. `llm_inference`

**语义**：调用远程语言模型推理端点（如 OpenAI、Anthropic、Azure OpenAI）。内核不实现 LLM 集成，仅通过合约边界路由推理请求。

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:llm-gpt4o",
    host_kind="llm_inference",
    network_policy="allow_list",
    allowed_hosts=["api.openai.com:443"],
)
```

**CapabilitySnapshot 关联**：LLM 推理动作应在 CapabilitySnapshot 中声明 `model_ref` 和 `model_content_hash`，防止模型静默替换：

```python
from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotInput

snapshot_input = CapabilitySnapshotInput(
    run_id="run-42",
    based_on_offset=0,
    tenant_policy_ref="policy:llm-v1",
    permission_mode="strict",
    model_ref="gpt-4o:20241120",
    model_content_hash="sha256:abc123...",
)
```

**安全边界**：
- `model_ref` 为内核合约字段，Executor 应校验实际调用模型与 `model_ref` 一致。
- API key 不应出现在 `action.input_json`；通过 Executor 层 vault/env 注入。
- 推理超时由 `Action.timeout_ms` 控制；超时 → `FailureEnvelope(failure_class="transient")`。

---

## 7. `remote_mcp_server`

**语义**：通过 MCP（Model Context Protocol）协议调用远程 MCP 服务器的 tool 操作。

**SandboxGrant 配置示例**：

```python
grant = SandboxGrant(
    grant_ref="grant:mcp-filesystem",
    host_kind="remote_mcp_server",
    network_policy="allow_list",
    allowed_hosts=["mcp-filesystem.internal:8080"],
)
```

**MCPActivityInput 配置**：

```python
from agent_kernel.kernel.contracts import MCPActivityInput

mcp_input = MCPActivityInput(
    run_id="run-42",
    action_id="action-99",
    server_name="filesystem",
    operation="read_file",
    arguments={"path": "/workspace/output.txt"},
)
```

**CapabilitySnapshot 关联**：MCP bindings 应通过 `CapabilitySnapshot.mcp_bindings` 字段声明，由 `CapabilityAdapter.resolve_mcp_bindings()` 解析：

```python
snapshot_input = CapabilitySnapshotInput(
    run_id="run-42",
    based_on_offset=0,
    tenant_policy_ref="policy:mcp-v1",
    permission_mode="strict",
    mcp_bindings=["filesystem", "github"],
)
```

**安全边界**：
- `operation` 须在接入层做白名单校验；禁止将 `operation` 直接来自外部用户输入。
- MCP 服务器连接池和服务发现属平台层责任；内核仅声明调用合约。
- `allowed_hosts` 须精确声明 MCP 服务器主机。

---

## 跨 host_kind 通用规则

### 超时与 Recovery 路径

所有 host_kind 的执行超时均通过 `Action.timeout_ms` 控制。超时后 Executor 应构造 `FailureEnvelope`：

```python
from agent_kernel.kernel.contracts import FailureEnvelope

envelope = FailureEnvelope(
    run_id="run-42",
    action_id="action-99",
    failed_stage="execution",
    failed_component="executor",
    failure_code="timeout",
    failure_class="transient",
    retryability="retryable",
)
```

Recovery 路径由 `RecoveryGateService.decide()` 决定：
- `failure_class="transient"` → 通常选择 `static_compensation` 或有界重试。
- `failure_class="deterministic"` → 通常选择 `abort`。
- `failure_class="side_effect"` → 通常需要 `human_escalation`。

### Admission 检查顺序

`StaticDispatchAdmissionService.admit()` 按以下顺序评估：

1. **peer_signal 白名单检查**：`action_type == "peer_signal"` 时验证 `target_run_id ∈ peer_run_bindings`。
2. **permission_mode 检查**：`permission_mode == "readonly"` 时拒绝写类动作。
3. **effect_class 检查**：`irreversible_write` 且 `permission_mode != "strict"` 时拒绝。

### AdmissionResult reason_code 参考

| reason_code | 含义 | 建议处理 |
|---|---|---|
| `ok` | 已批准 | 继续执行 |
| `permission_denied` | 权限不足 | 检查 `permission_mode` 与 `effect_class` |
| `quota_exceeded` | 配额超限 | 等待配额重置或升级策略 |
| `policy_denied` | 策略拒绝 | 检查 `tenant_policy_ref` |
| `dependency_not_ready` | 依赖未就绪 | 等待依赖完成后重试 |
| `stale_policy` | 策略过期 | 刷新 CapabilitySnapshot |
| `idempotency_contract_insufficient` | 幂等性合约不足以支持重试 | 改为 `human_escalation` |
| `peer_signal_missing_target` | `peer_signal` 动作缺少 `target_run_id` | 修复 `action.input_json` |
| `peer_signal_unauthorized_target` | target run 不在 `peer_run_bindings` 白名单 | 更新 CapabilitySnapshot |
