# Fake Implementation Remediation — Design Spec
**Date:** 2026-04-14
**Status:** Approved

---

## Problem Statement

A systematic audit identified 18 fake/stub/PoC implementations across production code paths. This document specifies the remediation for all Critical and High severity items, organized into 6 design components implemented via 4 parallel worktrees.

---

## Scope

| # | Component | Replaces | Severity |
|---|---|---|---|
| 1 | `SQLiteDecisionDeduper` | `InMemoryDecisionDeduper` | HIGH |
| 2 | `SnapshotDrivenAdmissionService` | `StaticDispatchAdmissionService` | CRITICAL |
| 3 | `AnthropicLLMGateway` + `OpenAILLMGateway` | `EchoLLMGateway` | HIGH |
| 4 | `SubprocessScriptRuntime` | `EchoScriptRuntime` | HIGH |
| 5 | `TemporalSDKWorkflowGateway.execute_turn` | `NotImplementedError` | CRITICAL |
| 6 | Bundle default wiring | all in-memory/static defaults | CRITICAL |

---

## Worktree Assignments

| Worktree | Components | Independence |
|---|---|---|
| WA | 1 (SQLiteDecisionDeduper) + 6 (bundle wiring) | Data layer; no cross-deps |
| WB | 2 (SnapshotDrivenAdmissionService) | Pure kernel logic; reads snapshot only |
| WC | 3 (AnthropicLLMGateway + OpenAILLMGateway) | External I/O; no kernel deps |
| WD | 4 (SubprocessScriptRuntime) + 5 (execute_turn) | I/O + Temporal substrate |

---

## Component 1: SQLiteDecisionDeduper

**File:** `agent_kernel/kernel/persistence/sqlite_decision_deduper.py`

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS decision_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
```

**Interface** (matches `DecisionDeduper` Protocol):
```python
class SQLiteDecisionDeduper:
    def __init__(self, database_path: str | Path) -> None: ...
    def contains(self, fingerprint: str) -> bool: ...
    def add(self, fingerprint: str, run_id: str = "") -> None: ...
```

**Semantics:**
- `contains`: `SELECT 1 FROM decision_fingerprints WHERE fingerprint = ?`
- `add`: `INSERT OR IGNORE INTO decision_fingerprints VALUES (?, ?, ?)`
- Both synchronous (matches existing Protocol); SQLite WAL mode for concurrent access.
- `database_path=":memory:"` allowed for tests only.

**Tests:**
- Unit: `contains` returns False before `add`, True after; duplicate `add` is idempotent.
- Integration: persist → new deduper instance from same path → `contains` still True.

---

## Component 2: SnapshotDrivenAdmissionService

**File:** `agent_kernel/kernel/admission/snapshot_driven_admission.py`

**Admission pipeline** (sequential; first failure → rejected):

### Rule 1 — Permission Mode
```
snapshot.permission_mode in {"readonly", "read_only"}
AND action.action_type NOT IN {"noop", "read"}
→ DENY, reason_code="permission_mode_readonly"
```

### Rule 2 — Binding Existence
```
action.action_type == "tool_call":
    action.tool_name NOT IN snapshot.tool_bindings.keys()
    → DENY, reason_code="tool_not_bound"

action.action_type == "mcp_call":
    (action.mcp_server, action.mcp_capability) NOT IN snapshot.mcp_bindings.keys()
    → DENY, reason_code="mcp_not_bound"

action.action_type == "peer_signal":
    snapshot.peer_run_bindings is not None
    AND action.peer_run_id NOT IN snapshot.peer_run_bindings
    → DENY, reason_code="peer_not_bound"
```

### Rule 3 — Idempotency Level
```
action.idempotency_level == "non_idempotent"
AND action.effect_class == "remote_write"
AND NOT policy.allow_non_idempotent_remote_writes
→ DENY, reason_code="non_idempotent_write_denied"
```

### Rule 4 — Risk Tier
```
action.risk_tier > policy.max_allowed_risk_tier
→ DENY, reason_code="risk_tier_exceeded"
```

### Rule 5 — Rate Limit
```
actions dispatched in last 60s > policy.max_actions_per_minute
→ DENY, reason_code="rate_limited"
```

**TenantPolicy format** (`agent_kernel/kernel/admission/tenant_policy.py`):
```python
@dataclass(frozen=True, slots=True)
class TenantPolicy:
    policy_id: str
    max_allowed_risk_tier: int = 3
    allow_non_idempotent_remote_writes: bool = False
    max_actions_per_minute: int = 120
```

Policy resolution: `tenant_policy_ref` is resolved by `TenantPolicyResolver`:
- `"policy:default"` → built-in conservative defaults (no file read)
- `"file:///path/to/policy.json"` → read JSON file, parse to `TenantPolicy`
- Other strings → raise `PolicyResolutionError`

**Rate limiting**: in-process sliding window per `run_id` using `collections.deque` of timestamps. Not shared across processes (acceptable; per-worker rate limiting).

**Tests:**
- Unit: one test per rule, each testing admit path and deny path.
- Integration: full TurnEngine cycle with real snapshot + admission.

---

## Component 3: LLM Gateways

### Config

**File:** `agent_kernel/kernel/cognitive/llm_gateway_config.py`
```python
@dataclass(frozen=True, slots=True)
class LLMGatewayConfig:
    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str
    max_tokens: int = 4096
    timeout_s: float = 60.0
    temperature: float = 0.0

    @classmethod
    def from_env(cls) -> LLMGatewayConfig:
        # AGENT_KERNEL_LLM_PROVIDER, AGENT_KERNEL_LLM_MODEL,
        # AGENT_KERNEL_LLM_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY)
        ...
```

**Factory:** `create_llm_gateway(config: LLMGatewayConfig) -> LLMGateway`

### AnthropicLLMGateway

**File:** `agent_kernel/kernel/cognitive/llm_gateway_anthropic.py`

- SDK: `anthropic.AsyncAnthropic(api_key=..., timeout=...)`
- `infer(context: ContextWindow) -> ModelOutput`:
  - Map `context.tool_definitions` → Anthropic `tools` list
  - Map `context.messages` → Anthropic `messages` list
  - Call `client.messages.create(model=..., tools=..., messages=..., max_tokens=...)`
  - Parse response: `content` blocks of type `tool_use` → `ToolCallOutput`; `text` → `TextOutput`
  - Return `ModelOutput(tool_calls=[...], text_content=..., stop_reason=...)`
- `count_tokens(context) -> int`:
  - Call `client.messages.count_tokens(...)` (beta endpoint)
  - Return `input_tokens`

### OpenAILLMGateway

**File:** `agent_kernel/kernel/cognitive/llm_gateway_openai.py`

- SDK: `openai.AsyncOpenAI(api_key=..., timeout=...)`
- `infer(context: ContextWindow) -> ModelOutput`:
  - Map `context.tool_definitions` → OpenAI `tools` (function-calling format)
  - Call `client.chat.completions.create(model=..., tools=..., messages=..., max_tokens=...)`
  - Parse `choices[0].message.tool_calls` → `ToolCallOutput` list
  - Return `ModelOutput`
- `count_tokens(context) -> int`:
  - Use `tiktoken` (lazy import) to estimate; fall back to `len(str(context)) // 4`

**Optional deps** (both are optional extras):
```toml
[project.optional-dependencies]
anthropic = ["anthropic>=0.40"]
openai = ["openai>=1.50", "tiktoken>=0.7"]
```

**Tests:**
- Unit: mock HTTP responses (`respx` / `unittest.mock`), verify message mapping and output parsing.
- No live API calls in CI.

---

## Component 4: SubprocessScriptRuntime

**File:** `agent_kernel/kernel/cognitive/script_runtime_subprocess.py`

```python
@dataclass(frozen=True, slots=True)
class SubprocessScriptConfig:
    timeout_s: float = 30.0
    max_output_bytes: int = 1_048_576
    python_executable: str = sys.executable
```

**`validate_script(script: str) -> bool`:**
```python
try:
    ast.parse(script)
    return True
except SyntaxError:
    return False
```

**`execute_script(request: ScriptActivityInput) -> ScriptResult`:**
```python
# 1. Validate syntax
if not validate_script(request.script):
    return ScriptResult(success=False, exit_code=1, error="SyntaxError")

# 2. Write to temp file
with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
    f.write(request.script.encode())
    tmppath = f.name

# 3. Execute subprocess
try:
    proc = await asyncio.create_subprocess_exec(
        config.python_executable, tmppath,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=config.timeout_s
    )
    stdout = stdout[:config.max_output_bytes]
    return ScriptResult(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        output=stdout.decode(errors="replace"),
        error=stderr.decode(errors="replace") if proc.returncode != 0 else "",
    )
except asyncio.TimeoutError:
    proc.kill()
    return ScriptResult(success=False, exit_code=-1, error="timeout")
finally:
    os.unlink(tmppath)
```

**Tests:**
- Unit: hello-world script returns success; syntax error returns `success=False`; timeout returns `success=False, error="timeout"`.
- No mocking of subprocess (real subprocess is fast enough for tests).

---

## Component 5: TemporalSDKWorkflowGateway.execute_turn

**Root cause:** `execute_turn` cannot serialize a Python callable across process boundaries.

**Resolution:** `execute_turn` on the gateway does NOT dispatch a new Temporal activity. Instead:
- It resolves the handler to a registered `tool_name` or MCP key
- Delegates to `TemporalSDKActivityGateway.execute_tool` / `execute_mcp`
- The activity gateway is injected into the workflow gateway at construction time

**Implementation:**
```python
async def execute_turn(
    self,
    run_id: str,
    action: Any,
    handler: Any,
    *,
    idempotency_key: str,
) -> Any:
    if self._activity_gateway is None:
        raise RuntimeError(
            "execute_turn requires an activity_gateway; "
            "pass activity_gateway= to TemporalSDKWorkflowGateway."
        )
    action_type = getattr(action, "action_type", None)
    if action_type == "tool_call":
        return await self._activity_gateway.execute_tool(
            ToolActivityInput(
                tool_name=action.tool_name,
                arguments=action.arguments,
                idempotency_key=idempotency_key,
                run_id=run_id,
            )
        )
    if action_type == "mcp_call":
        return await self._activity_gateway.execute_mcp(
            MCPActivityInput(
                server_name=action.mcp_server,
                operation=action.mcp_capability,
                arguments=action.arguments,
                idempotency_key=idempotency_key,
                run_id=run_id,
            )
        )
    raise ValueError(f"execute_turn: unsupported action_type={action_type!r}")
```

**`TemporalSDKWorkflowGateway.__init__`** gains optional `activity_gateway: TemporalActivityGateway | None = None` parameter.

**HTTP layer:** `post_run_turn` 501 stub is replaced with real routing when `substrate_type == "temporal"` and `activity_gateway` is present. Falls back to 501 otherwise.

**Tests:**
- Unit: mock activity_gateway, verify tool_call and mcp_call routing.
- Integration: end-to-end with `TemporalSDKActivityGateway` + registered handlers.

---

## Component 6: Bundle Default Wiring

**`build_minimal_complete` parameter changes:**

```python
# NEW parameters
llm_gateway_config: LLMGatewayConfig | None = None,
script_config: SubprocessScriptConfig | None = None,
admission_config: AdmissionConfig | None = None,
```

**Default behaviour change:**

| Field | Before | After |
|---|---|---|
| `deduper` | `InMemoryDecisionDeduper()` | `SQLiteDecisionDeduper(data_dir/decision_deduper.db)` |
| `admission` | `StaticDispatchAdmissionService()` | `SnapshotDrivenAdmissionService(policy_resolver)` |
| `llm_gateway` | caller provides `EchoLLMGateway` | `create_llm_gateway(LLMGatewayConfig.from_env())` if env set, else `None` |
| `script_runtime` | caller provides `EchoScriptRuntime` | `SubprocessScriptRuntime(script_config)` |

**Production safety additions:**
```python
if isinstance(deduper, InMemoryDecisionDeduper):
    violations.append("deduper InMemoryDecisionDeduper is not allowed in prod")
if isinstance(llm_gateway, EchoLLMGateway):
    violations.append("llm_gateway EchoLLMGateway is not allowed in prod")
if isinstance(script_runtime, EchoScriptRuntime):
    violations.append("script_runtime EchoScriptRuntime is not allowed in prod")
```

**`KernelConfig` new fields:**
```
AGENT_KERNEL_LLM_PROVIDER      → llm_provider: str = ""
AGENT_KERNEL_LLM_MODEL         → llm_model: str = ""
AGENT_KERNEL_LLM_API_KEY       → llm_api_key: str = ""
AGENT_KERNEL_SCRIPT_TIMEOUT_S  → script_timeout_s: float = 30.0
```

---

## Testing Strategy

R6 (Three-Layer) compliance per component:

| Component | Unit | Integration | E2E |
|---|---|---|---|
| SQLiteDecisionDeduper | ✓ pure logic | ✓ restart persistence | — |
| SnapshotDrivenAdmission | ✓ each rule | ✓ TurnEngine cycle | ✓ HTTP POST /runs |
| LLM Gateways | ✓ mocked HTTP | — | — (no live API in CI) |
| SubprocessScriptRuntime | ✓ real subprocess | — | — |
| execute_turn | ✓ mocked gateway | ✓ activity gateway | — |
| Bundle wiring | ✓ config parsing | ✓ full bundle startup | ✓ health probe |

---

## Files Changed Summary

**New files:**
- `agent_kernel/kernel/persistence/sqlite_decision_deduper.py`
- `agent_kernel/kernel/admission/__init__.py`
- `agent_kernel/kernel/admission/snapshot_driven_admission.py`
- `agent_kernel/kernel/admission/tenant_policy.py`
- `agent_kernel/kernel/cognitive/llm_gateway_config.py`
- `agent_kernel/kernel/cognitive/llm_gateway_anthropic.py`
- `agent_kernel/kernel/cognitive/llm_gateway_openai.py`
- `agent_kernel/kernel/cognitive/script_runtime_subprocess.py`

**Modified files:**
- `agent_kernel/runtime/bundle.py` (deduper + admission + llm + script wiring)
- `agent_kernel/config.py` (llm_provider, llm_model, llm_api_key, script_timeout_s)
- `agent_kernel/substrate/temporal/gateway.py` (execute_turn implementation)
- `agent_kernel/service/http_server.py` (post_run_turn routing)
- `pyproject.toml` (anthropic, openai optional extras)

**Test files (new):**
- `python_tests/agent_kernel/kernel/persistence/test_sqlite_decision_deduper.py`
- `python_tests/agent_kernel/kernel/admission/test_snapshot_driven_admission.py`
- `python_tests/agent_kernel/kernel/cognitive/test_llm_gateway_anthropic.py`
- `python_tests/agent_kernel/kernel/cognitive/test_llm_gateway_openai.py`
- `python_tests/agent_kernel/kernel/cognitive/test_script_runtime_subprocess.py`
- `python_tests/agent_kernel/substrate/test_temporal_execute_turn.py`
