# agent-kernel 演进工作计划

> 基于 [ARCHITECTURE.md](ARCHITECTURE.md) 中 ○ 标注模块，按依赖顺序分阶段拆解。
>
> **当前基线**：0.1 · 六权威协议 ✓ · 6 235 测试通过 · 串行单动作执行

---

## 总体思路

当前内核已具备完整的**执行治理层**（六权威、幂等、持久化、可观测）。
下一步演进方向是**认知驱动**：让大模型成为内核的决策引擎，而不是由平台直接注入 Action。

演进路径遵循三条原则：

1. **依赖前序** — 每阶段只依赖已实现的模块，不跳步。
2. **协议先行** — 先在 `contracts.py` 定义 Protocol/DTO，再写实现，测试针对 Protocol 编写。
3. **PoC 可运行** — 每阶段结束时必须有端到端可运行的 PoC，不只是合约定义。

---

## Phase 1：认知基底（LLM 接入层）

**目标**：让内核能够通过 LLMGateway 发起一次真实的模型推理，并将输出解析成 `Action`。

**为什么先做这个**：所有后续能力（ReasoningLoop / 并行执行 / 反思闭环）都依赖 LLM 调用链路的正确性。协议定义清楚后，后续实现只需替换/扩展，不用改架构。

### 1.1 合约定义（`kernel/contracts.py` 扩展）

| 新增合约 | 关键字段 | 说明 |
|---------|---------|------|
| `TokenBudget` | `max_input / max_output / reasoning_budget` | 每 Turn 可配置，reasoning 轮 vs tool 选择轮预算差异大 |
| `InferenceConfig` | `model_ref / token_budget / temperature / turn_kind_overrides` | 进入 SHA256 审计链（含 CapabilitySnapshot） |
| `ContextWindow` | `system_instructions / tool_definitions / skill_definitions / history / current_state / memory_ref / recovery_context` | 装配后的模型输入，不可变 |
| `ModelOutput` | `raw_text / tool_calls / finish_reason / usage` | 归一化的模型响应 |
| `LLMGateway` (Protocol) | `infer() / count_tokens() / stream_infer()` | 南向 Provider 抽象，位于 Temporal Activity 内 |
| `ContextPort` (Protocol) | `assemble(run_id, snapshot, history) → ContextWindow` | 上下文工程接口，平台侧实现 |
| `OutputParser` (Protocol) | `parse(output, snapshot) → list[Action] \| ExecutionPlan` | 模型输出 → 执行意图 |

**关键约束**：`InferenceConfig` 必须进入 `CapabilitySnapshot` 的 SHA256 哈希，确保同一快照下推理配置可审计。

### 1.2 南向 Activity（`substrate/temporal/activity_gateway.py` 扩展）

```
execute_inference(InferenceActivityInput) → ModelOutput
  InferenceActivityInput:
    context_window: ContextWindow
    config: InferenceConfig
    idempotency_key: str       ← Temporal Activity 级别幂等
    run_id: str
```

- 幂等键由 `(run_id, turn_id, attempt_seq)` 构成，Temporal retry 不重复计费。
- 两级 retry 必须分离：Temporal Activity retry（内核级）+ LLMGateway 内部 retry（Provider 级 rate limit）。

### 1.3 LLMGateway PoC 实现

优先实现两个 Provider：
- `OpenAILLMGateway`：兼容 OpenAI API（含 Azure / Deepseek / Qwen 等兼容端点）
- `AnthropicLLMGateway`：调用 Claude API

共同规范：
- `infer()` 返回归一化 `ModelOutput`，屏蔽 Provider 差异。
- `count_tokens()` 支持预算检查（在 `execute_inference` 调用前估算）。
- Provider 错误分类：`rate_limit_error` / `context_length_exceeded` / `model_unavailable` → 分别路由到不同 retry 策略。

### 1.4 OutputParser PoC 实现

- `ToolCallOutputParser`：OpenAI function calling / Anthropic tool_use → `list[Action]`
- `JSONModeOutputParser`：结构化 JSON → `list[Action]`（作为降级方案）

### 1.5 测试目标

- `LLMGateway` 测试：mock Provider SDK，验证归一化、幂等键透传、错误分类。
- `OutputParser` 测试：给定原始模型响应，验证解析出的 `Action` 字段正确性。
- `execute_inference` Activity 测试：mock Gateway，验证 Activity 边界和幂等键生成。
- `InferenceConfig` 进入 `CapabilitySnapshot` SHA256：快照哈希变更验证。

**完成标志**：可以在 pytest 中用 mock LLMGateway 驱动一次完整的 `ContextWindow → ModelOutput → Action[]` 流程。

---

## Phase 2：ReasoningLoop 集成

**目标**：让 `RunActorWorkflow` 能够通过 ReasoningLoop 驱动模型，替代平台直接注入 Action。

**依赖**：Phase 1 全部完成。

### 2.1 ReasoningLoop 服务（`kernel/reasoning_loop.py`）

```python
class ReasoningLoop:
    """
    单次推理-解析周期。
    输入：run_id + snapshot + 历史事件
    输出：list[Action] 或 ExecutionPlan（含并行组，Phase 3 实现）
    """
    async def run_once(
        self,
        run_id: str,
        snapshot: CapabilitySnapshot,
        context_port: ContextPort,
        llm_gateway: LLMGateway,
        output_parser: OutputParser,
        config: InferenceConfig,
    ) -> list[Action]: ...
```

### 2.2 TurnEngine 新增 `reasoning` 状态

```
collecting ──► reasoning ──► intent_committed ──► ...（后续路径不变）
```

- `reasoning` 状态触发 `execute_inference` Activity。
- `intent_committed` 收到 `list[Action]` 后，后续路径与当前完全相同（六权威治理不变）。
- 平台注入 Action（现有路径）保留，`reasoning` 是可选路径，由 `CapabilitySnapshot.inference_config` 是否存在决定。

### 2.3 RunActorDependencyBundle 扩展

新增可注入字段：
```python
context_port: ContextPort | None = None
llm_gateway: LLMGateway | None = None
output_parser: OutputParser | None = None
```

无注入时 TurnEngine 跳过 `reasoning` 状态，走现有平台注入路径（向后兼容）。

### 2.4 测试目标

- `ReasoningLoop.run_once()` 单元测试：mock ContextPort + LLMGateway + OutputParser。
- `TurnEngine` 集成测试：注入 mock ReasoningLoop，验证 `reasoning → intent_committed` 状态转换。
- 端到端 PoC：`KernelRuntime.start()` 注入真实 OpenAI Gateway，完成一次带模型推理的 Turn。

**完成标志**：`TurnEngine` 可以由模型驱动完成一个 Turn，整个过程可通过 EventLog replay 确定性重放。

---

## Phase 3：并行执行模型

**目标**：让模型可以输出并行执行计划，内核用 Temporal 原生并发执行，并在 join 点聚合结果。

**依赖**：Phase 2 完成（OutputParser 需要能解析 `ExecutionPlan`）。

### 3.1 合约定义（`kernel/contracts.py` 扩展）

```python
@dataclass(frozen=True, slots=True)
class ParallelGroup:
    actions: tuple[Action, ...]
    join_strategy: Literal["all", "any", "n_of_m"]
    n: int | None = None
    timeout_ms: int | None = None
    group_idempotency_key: str = ""   # 组级幂等

@dataclass(frozen=True, slots=True)
class ParallelPlan:
    groups: tuple[ParallelGroup, ...]

ExecutionPlan = SequentialPlan | ParallelPlan  # Union type

@dataclass(frozen=True, slots=True)
class BranchResult:
    action_id: str
    outcome: ActionOutcome
    output_json: dict | None = None

@dataclass(frozen=True, slots=True)
class BranchFailure:
    action_id: str
    failure_kind: str
    evidence: FailureEnvelope
```

### 3.2 PlanExecutor（`kernel/plan_executor.py`）

```
SequentialPlan → 委托给现有 TurnEngine 逐 Turn 执行（不变）

ParallelPlan →
    for group in plan.groups:
        results = await asyncio.gather(
            *[_execute_branch(action) for action in group.actions],
            return_exceptions=True,
        )
        join(results, group.join_strategy) → BranchResult[] + BranchFailure[]
```

- `asyncio.gather` 在 Temporal Workflow 上下文中即为原生并发 Activity。
- `join_strategy="all"` 时任一失败 → 整组进 RecoveryGate。
- `join_strategy="any"` 时首个成功即可推进，其余取消。
- 每个 Action 保留独立幂等键，组级重试不会重复已完成的 Action。

### 3.3 TurnEngine 并行状态扩展

```
dispatched ──► parallel_dispatched ──► parallel_joined ──► effect_recorded
                                    └► parallel_partial_failure ──► recovery_pending
```

- `parallel_dispatched`：PlanExecutor 已提交 asyncio.gather，等待聚合。
- `parallel_joined`：所有分支满足 join_strategy，事件记录各分支结果。
- `parallel_partial_failure`：收集 BranchFailure，进入 RecoveryGate。

### 3.4 幂等保证

- `join_strategy="all"`：组级幂等键，组失败则整组重入（已完成分支的幂等键防止重复执行）。
- `join_strategy="any"`：每 Action 独立幂等键，join 后其余 Action 的 DedupeStore 状态标记为 `cancelled`。

### 3.5 测试目标

- `PlanExecutor` 单元测试：mock TurnEngine，验证 gather 和 join 语义。
- `ParallelGroup` 幂等测试：模拟部分分支失败后重试，已完成分支不重复执行。
- `parallel_partial_failure → recovery_pending` 状态转换测试。

**完成标志**：一个包含 3 个并行 Action 的 `ParallelPlan` 可以在 mock TurnEngine 中正确执行和聚合，部分失败正确进入恢复路径。

---

## Phase 4：BranchMonitor + 脚本执行基础设施

**目标**：为脚本执行提供安全容器（子进程 + 心跳），检测死循环，收集结构化故障证据。

**依赖**：Phase 3 并行执行基础（BranchFailure/BranchResult 合约）。

### 4.1 ScriptRuntime Protocol（`kernel/contracts.py` 扩展）

```python
class ScriptRuntime(Protocol):
    async def execute_script(
        self,
        script_id: str,
        script_content: str,
        parameters: dict,
        timeout_ms: int,
        heartbeat_interval_ms: int,
    ) -> ScriptResult: ...

    async def validate_script(
        self,
        script_content: str,
        host_kind: HostKind,
    ) -> ValidationResult: ...
```

`host_kind` 路由：
- `local_process` → 子进程（subprocess）
- `in_process_python` → `exec()` in isolated namespace
- `remote_service` → HTTP/gRPC 调用远端执行器

### 4.2 execute_skill_script Activity（`substrate/temporal/activity_gateway.py` 扩展）

```python
async def execute_skill_script(input: ScriptActivityInput) -> ScriptResult:
    """
    Temporal Activity wrapper：
    1. 启动子进程执行脚本
    2. 每 heartbeat_interval_ms 调 activity.heartbeat(budget_consumed_ratio)
    3. 超时 → kill 子进程 → 构建 ScriptFailureEvidence
    4. 正常结束 → 返回 ScriptResult
    """
```

### 4.3 ScriptFailureEvidence（`kernel/failure_evidence.py` 扩展）

```python
@dataclass(frozen=True, slots=True)
class ScriptFailureEvidence:
    script_id: str
    failure_kind: Literal[
        "heartbeat_timeout",
        "runtime_error",
        "permission_denied",
        "resource_exhausted",
        "output_validation_failed",
    ]
    budget_consumed_ratio: float      # ≈1.0 时为死循环特征
    output_produced: bool             # False + ratio≈1.0 → 疑似死循环
    suspected_cause: str | None       # "possible_infinite_loop" / "oom" / ...
    partial_output: str | None        # 执行到超时时的部分输出（如有）
    original_script: str              # 原始脚本（供反思模型对比）
    stderr_tail: str | None           # 最后 N 行 stderr
```

### 4.4 BranchMonitor（`kernel/branch_monitor.py`）

```
每个 Activity / Child Workflow → 独立 heartbeat 追踪
    ├── 正常心跳 → 更新 last_heartbeat_at
    ├── 心跳超时 → 注入 branch_heartbeat_timeout 信号
    └── 子进程无输出 + budget_consumed_ratio ≈ 1.0 → suspected_cause = "infinite_loop"
```

`BranchMonitor` 不是新的权威，是 `RunHeartbeatMonitor` 的分支级扩展，集成进 `PlanExecutor`。

### 4.5 测试目标

- `execute_skill_script` Activity 测试：mock 子进程，验证心跳发送频率和超时 kill。
- `ScriptFailureEvidence` 构建测试：死循环场景（ratio≈1.0 + output_produced=False）。
- `BranchMonitor` 测试：模拟心跳超时，验证信号注入。

**完成标志**：一个死循环脚本执行后，Activity 超时，正确构建 `ScriptFailureEvidence(suspected_cause="possible_infinite_loop")`，进入下游恢复路径。

---

## Phase 5：反思闭环（reflect_and_retry）

**目标**：当脚本执行失败时，将结构化故障证据送入模型进行反思，模型输出修正脚本后重新执行。

**依赖**：Phase 4（ScriptFailureEvidence）+ Phase 2（ReasoningLoop）。

### 5.1 新增 RecoveryMode 和合约

```python
# kernel/contracts.py 扩展
RecoveryMode = Literal[
    "static_compensation",
    "human_escalation",
    "abort",
    "reflect_and_retry",   # NEW ○
]

@dataclass(frozen=True, slots=True)
class ReflectionPolicy:
    max_rounds: int = 3
    reflection_timeout_ms: int = 60_000
    reflectable_failure_kinds: frozenset[str] = frozenset({
        "heartbeat_timeout", "runtime_error", "output_validation_failed",
    })
    non_reflectable_failure_kinds: frozenset[str] = frozenset({
        "resource_exhausted", "permission_denied",
    })
    escalate_on_exhaustion: bool = True   # 超出 max_rounds → human_escalation
```

### 5.2 ReflectionContextBuilder（`kernel/recovery/reflection_builder.py`）

```python
class ReflectionContextBuilder:
    """
    ScriptFailureEvidence + 原始脚本 + 成功分支结果
    → ContextWindow 增量（结构化故障上下文）
    
    输出给模型看到的内容：
      - 失败类型和原因
      - 原始脚本（可视化展示）
      - 部分输出（如有）
      - 成功分支的结果（并行场景）
      - 明确指令："请分析失败原因并输出修正后的脚本"
    """
    def build(
        self,
        evidence: ScriptFailureEvidence,
        successful_branches: list[BranchResult],
        base_context: ContextWindow,
    ) -> ContextWindow: ...  # 返回增量合并后的新 ContextWindow
```

### 5.3 reflect_and_retry 执行流（`kernel/recovery/gate.py` 扩展）

```
parallel_partial_failure 事件 → RecoveryGate 决策
    ↓ mode = reflect_and_retry
    ↓ 检查 ReflectionPolicy.reflectable_failure_kinds ∩ evidence.failure_kind
    ↓ 检查 reflection_round < max_rounds（从 TurnIntentLog 读，重启后不丢失）
ReflectionContextBuilder.build() → 增量 ContextWindow
    ↓
ReasoningLoop.run_once()（模型推理，接收故障证据）
    ↓ 模型输出：故障分析 + 修正脚本
OutputParser.parse() → 新 Action（修正后脚本）
    ↓
TurnEngine 进入 reflecting 状态
    ↓ 重入 intent_committed（带修正 Action）
    ↓ 后续路径走完整六权威管道（不跳过 Admission / DedupeStore）

成功 → 继续
再次失败 → reflection_round + 1（写入 TurnIntentLog）
round > max_rounds → escalate_on_exhaustion ? human_escalation : abort
```

### 5.4 TurnEngine 新增 `reflecting` 状态

```
recovery_pending ──► reflecting ──► intent_committed（修正 Action）
                               └──► human_escalation / abort（轮次耗尽）
```

**关键**：`reflecting` 不是新权威，是 RecoveryGate 内部的状态转换。六权威结构不变。

### 5.5 反思轮次持久化

`TurnIntentLog` 扩展：新增 `reflection_round: int = 0` 字段。  
进程重启后从 `TurnIntentLog` 读取已消耗轮次，防止计数器归零导致无限反思。

### 5.6 测试目标

- `ReflectionContextBuilder` 测试：给定 `ScriptFailureEvidence`，验证构建的 `ContextWindow` 包含正确的故障上下文。
- `ReflectionPolicy` 验证测试：non_reflectable 类型不触发反思，轮次耗尽后正确 escalate。
- `reflect_and_retry` 端到端测试：死循环脚本 → 故障 → 反思 → 修正脚本 → 成功（mock 模型）。
- TurnIntentLog 持久化测试：模拟进程重启，reflection_round 正确恢复。

**完成标志**：死循环脚本经过模型反思后输出修正版本，修正版本通过六权威管道重新执行，整个过程有完整 EventLog 记录且可确定性重放。

---

## Phase 6：技能驱动完整链路

**目标**：将 Phase 1-5 的所有模块串联，实现平台注入技能定义 → 模型决策调用技能 → 内核治理执行 → 结果返回的完整闭环。

**依赖**：Phase 1-5 全部完成。

### 6.1 技能注入（平台 → 内核）

- `ContextPort.assemble()` 将 `skill_bindings` 序列化为模型可理解的工具/技能定义：
  - 技能 ID、描述、参数 schema
  - 可用脚本清单（仅当前 run 有权访问的）
  - 预期输出格式
- `CapabilitySnapshot.skill_bindings` 已在 SHA256 审计链中（现有 ✓）。

### 6.2 技能调用（模型决策）

```python
# OutputParser 解析出：
Action(
    action_type="skill_call",
    skill_id="data_analysis_v1",
    effect_class="tool_executor",
    interaction_target="tool_executor",
    input_json={"dataset_ref": "...", "analysis_type": "..."},
)
```

`DispatchAdmission` 检查：`skill_id ∈ snapshot.skill_bindings`（已有 Admission 框架，扩展规则）。

### 6.3 技能生命周期路由（`skills/runtime_factory.py` 扩展）

```
skill_call Action 进入 ExecutorService
    ├── 复杂技能（has_sub_workflow=True）
    │     → SpawnChildRunRequest → Child RunActorWorkflow（现有 ✓）
    │       子 Run 有独立 ContextWindow，模型驱动内部脚本序列
    └── 原子技能（has_sub_workflow=False）
          → execute_skill_script Activity ○（Phase 4 实现）
            → ScriptRuntime ○ 路由到 host_kind 执行机制
```

### 6.4 子 Run 内脚本执行（模型驱动）

子 Run 的 `CapabilitySnapshot.skill_bindings` 包含该技能的脚本清单：

```python
# 子 Run 内模型输出：
Action(
    action_type="script_execution",
    script_id="analyze_data.py",
    host_kind="in_process_python",
    effect_class="data_system",
    input_json={"dataset_ref": "...", "params": {...}},
)
```

脚本执行走完整六权威管道：Admission → DedupeStore → `execute_skill_script` Activity → ScriptRuntime。  
失败 → `ScriptFailureEvidence` → `reflect_and_retry`（Phase 5）。

### 6.5 串行 vs 并行脚本

- 串行：子 Run 的 TurnEngine 逐 Turn 执行（现有 ✓）。
- 并行：模型输出 `ParallelPlan`，`PlanExecutor` 并发执行（Phase 3 ○）。
- 混合：`ExecutionPlan` 中 sequential steps 里嵌套 parallel groups。

### 6.6 测试目标

- 端到端技能调用测试：平台注入技能定义 → mock 模型输出 `skill_call` Action → 路由到 Child Workflow → 子模型输出脚本 Action → 执行 → 结果回传父 Run。
- 并行脚本技能测试：技能内含 `ParallelPlan`，多脚本并发执行，join 后合并结果。
- 反思闭环技能测试：子 Run 内脚本死循环 → 反思 → 修正 → 成功。

**完成标志**：一个完整的技能调用链路（平台→内核→子Run→脚本→模型→结果）全程由六权威治理，EventLog 完整记录，可重放。

---

## 优先级矩阵

| Phase | 核心价值 | 前置依赖 | 风险点 |
|-------|---------|---------|-------|
| 1 认知基底 | 接入真实 LLM，内核从"工具执行器"进化为"认知执行器" | 无 | Provider SDK 差异大，归一化设计要稳 |
| 2 ReasoningLoop | 模型驱动 Turn，内核自主决策 | Phase 1 | Temporal 确定性约束与 LLM 随机性的边界 |
| 3 并行执行 | 多 Action 并发，吞吐量数量级提升 | Phase 2 | join_strategy 幂等性、并发状态机正确性 |
| 4 BranchMonitor | 脚本安全容器，检测死循环 | Phase 3 | 子进程心跳跨 OS 兼容性（Windows/Linux） |
| 5 反思闭环 | 自修复能力，减少人工干预 | Phase 2 + 4 | 防止无限反思循环，ReflectionPolicy 调参 |
| 6 技能全链路 | 完整 E2E 流程，平台可接入 | Phase 1-5 全 | 技能版本兼容、子 Run 生命周期管理 |

---

## 关键设计决策备忘

### LLM 推理与 Temporal 的关系

`execute_inference` 是一个 **Temporal Activity**，不是单独的服务：
- Temporal Activity 提供持久化保证（崩溃重启后 Activity 重新调度，不丢失 Turn）。
- LLMGateway 在 Activity **内部**，处理 Provider 级 rate limit retry（两级 retry 不合并）。
- 模型调用有 `idempotency_key`，Temporal retry 不会重复计费。

### 上下文工程的不变量

```
上下文质量 → 大模型输出的上界
大模型输出 ← f(上下文质量, 模型能力)  // 两者不可互推
```

`ContextPort` 是内核对上下文工程的唯一接口。平台不能绕过 `ContextPort` 直接拼 prompt。  
这是不变量 9（ARCHITECTURE.md §十一），后续合规审计依赖此约束。

### 反思轮次的持久化

`reflection_round` 存在 `TurnIntentLog` 中，不在内存里。原因：进程重启后计数器归零 = 永久反思循环。  
`TurnIntentLog` 已有 SQLite 后端（✓），只需加字段，不需要新存储。

### 并行幂等的边界条件

- `join_strategy="all"` 失败重试时：DedupeStore 的 `dispatched` 状态保护已完成分支，不重复执行。
- `join_strategy="any"` 首个成功后：其余 Action 的幂等键状态标记为 `cancelled`（DedupeStore 新增终态）。
- `cancelled` 是不可逆终态，与 `acknowledged` 平行，不参与重试路径。
