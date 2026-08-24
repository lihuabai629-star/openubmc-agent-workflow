# openUBMC Agent Workflow 架构裁决

日期：2026-08-19
设计基线：`refactor/agent-semantic-gateway`，`db38a4a`
实现状态：核心裁决已在 `refactor/run-engine-core` 落地，剩余项见第 13 节

## 1. 最终裁决

保留当前产品方向：一个持久化 Runtime Core、两个默认 Agent operations（`observe`、`execute`）和独立 Operator/CI Plane。当前不应恢复 19 个 Agent-facing 工具，也不应引入 Temporal、BPMN、通用 DAG 或 Agent 可见的命令轮询协议。

需要纠正的是 Runtime 内部的 seam：

1. `observe/execute` 是外部 Agent Interface，不是内部万能 JSON 抽象；Gateway 必须把输入立即解码成 typed Query/Command。
2. 将现有 `WorkflowKernel` 更名并收缩为 `WorkflowDefinitions`；它只负责不可变定义、版本固定、步骤身份和语义指纹。
3. 提取唯一状态转换权威 `RunEngine`。不能同时保留一个“WorkflowKernel 状态机”和一个“RunEngine 状态机”。
4. `execute` 继续返回推进到下一真实 Gate、Incident 或 Outcome 的 `Turn`；`CommandAck` 与 polling 只能作为未来 transport/worker Adapter 的内部机制。
5. 当前不引入 Outbox/Inbox、Broker 或独立 Worker。现有单进程同步调用没有跨进程消息 dual-write；`MutationJournal` 已承担危险副作用的 durable identity、effect-start boundary 和 reconcile。
6. 采用局部 event-backed Run/Effect ledger，不采用全面 Event Sourcing/CQRS。正常恢复使用物化投影，事件用于审计、验证和必要重建。
7. `Gate` 表示预期的外部输入；`Incident` 表示自动推进无法安全继续的异常。v2 先把所有未知 Mutation fail closed，v2.1 再把 Incident 固化为领域对象。
8. 模型推理继续留在 Runtime 外。未来若 Runtime 主动调用模型，该调用必须被视为非确定性 Effect，输出持久化为 Artifact，恢复时不得重新推理替代已记录结果。

一句话目标架构：

> Agent 每次只处理 Observation、真实决策 Gate、Incident 或最终 Outcome；Runtime 吸收定义解析、状态推进、幂等、副作用、恢复、验证和持久化复杂度。

## 2. 当前证据与问题定位

### 2.1 已成立的方向

- 默认 Agent profile 已收缩为 `observe/execute`，兼容面与治理面分离，详见 [Agent Semantic Gateway](agent-semantic-gateway.md)。
- 正式 10 对 A/B 中，新 Observation Interface 的总 Token、非缓存输入加输出、耗时比率分别为 `0.234815`、`0.320367`、`0.371208`。这证明语义收缩有效，但尚不能证明完整 `execute` 与 Mutation 恢复合格。
- 当前 Runtime 已具备 Case/Event、Evidence、workflow definition、MutationJournal、幂等、目标 lease、target epoch、fresh verification、reconcile、Replay 和 Outcome 等有价值资产。

市场方法与一手资料的完整比较见 [市场工作流方法调研](workflow-design-market-research.md)。本裁决不重复产品调研，只解决实现中尚未统一的架构分歧。

### 2.2 状态权威目前名实不符

迁移前的 `WorkflowKernel` 只完成三件事：

- 根据投影解析或恢复 `WorkflowDefinition`；
- 生成 `StepIdentity`；
- 生成 semantic cursor。

迁移前，真正的 Run 推进、Gate 产生、Domain 调用、unknown Mutation 阻断、事件提交和
Closeout 均位于 `ContextRuntime.workflow_advance`，`workflow.next` 只是其 continuation
入口。当前 Agent 主路径已由 `RunEngine.execute` 选择 Gate 或单个 Domain step，不再调用
`workflow.next`；`ContextRuntime` 保留 definition cursor、event repository 和 Domain
invocation Adapter。当前 Agent Gate response 由 `RunEngine` 写入原生
`RunGateSubmitted`，旧事件只通过显式 upcaster 进入统一投影；兼容 writer/profile 的
候选移除已完成，但在证据门禁通过前不得进入 canonical `main`。

因此实现没有增加第二个状态机，而是把 Agent-visible transition authority 从巨型
`ContextRuntime` 纵向迁到 `RunEngine`。`WorkflowKernel` 已校正为
`WorkflowDefinitions`，旧名称只保留 import alias。

### 2.3 外部 Interface 已变深，内部 seam 仍浅

- 迁移前的 `AgentGatewayRuntimePort` 暴露 8 个方法，Gateway 需要知道 observation
  refinement、持久化、Run 启动、snapshot、reconcile 和 Outcome 记录。
- 迁移前 `respond` 由 Gateway 组合 `phase_record` 与 `workflow.next`，terminal Outcome
  又由 Gateway 单独写入。
- 当前 `SemanticRuntimePort` 只保留 typed `observe/execute`；Gateway 仅解码和投影，
  `RunEngine` 负责 Gate、推进、reconcile、Incident 和 Outcome。
- 当前 `DomainExecutor` 在 Runtime 构造时预注册 Adapter，并按 Effect class 选择有限只读
  重试或单次 Mutation 执行。
- Agent request 与 stdio frame 已设置 256 KiB 总预算，大内容通过 ObservationRef 或
  ArtifactRef 流转。

### 2.4 Mutation 语义已经比通用工作流更成熟

[`run_mutation`](../openubmc-target-runtime/openubmc_target_runtime/runtime.py#L2007) 已经实现：

1. 授权与 operation fingerprint 检查；
2. 同 target unfinished journal 阻断；
3. target-exclusive mutation lease；
4. `planned → applying → applied → verifying → verified`；
5. effect 前失败进入 `replan_required`；
6. effect 可能开始后失败进入未知结果；
7. target epoch 前进后进行 fresh verification；
8. recovery 先做只读 inspection，再选择 replan、verify、rollback 或 manual。

[`MutationJournal.mark_effects_started`](../openubmc-target-runtime/openubmc_target_runtime/mutation.py#L531) 是安全边界。它不是普通 workflow event 的同义词，也不能被一个通用 Outbox 状态机替代。

## 3. Design It Twice：三种 Interface 方案

### 方案 A：继续以 Gateway 为协调中心

```text
AgentGateway
  ├─ observe_operation
  ├─ assure_observation
  ├─ persist_observation
  ├─ start_run
  ├─ run_operation
  ├─ run_snapshot
  ├─ reconcile_run
  └─ record_run_outcome
```

优点是迁移成本低。缺点是 Gateway 必须理解 Runtime 的操作顺序、恢复和持久化，Interface 与 Implementation 同样复杂；删除该 Adapter 后，复杂度不会集中出现于一个 Module，而会散回 Gateway 和 MCP。该方案 depth 不足，否决。

### 方案 B：Agent 可见的异步 Command Bus

```text
execute(Command) -> CommandAck
observe(run status) / poll(run_id) -> Snapshot
Outbox -> Broker -> Worker -> Inbox -> Run events
```

它适合跨进程 Worker、独立扩缩容和 active-active Runtime，但会让 Agent 学习 accepted、queued、running、retry-after、poll、stale result 等调度概念。当前没有 Broker、跨进程 Worker、共享 fencing 或吞吐瓶颈证据，引入后会显著扩大运维和故障测试面。当前否决，保留为 v3 候选。

### 方案 C：typed command 到下一语义 yield

```text
MCP / CLI Adapter
       │ JSON decode / bounded validation
       ▼
Agent Gateway
       │ typed Query / RunCommand
       ▼
Runtime Module
  observe(...) -> ObservationResult
  execute(...) -> Turn
```

Runtime 内部可以同步执行，也可以将来通过 queue/worker 异步执行；这些机制都被 Adapter 隐藏。Agent 一次调用只在下一真实 Gate、Incident、running reattach point 或 Outcome 返回。

该方案兼顾当前调用路径的低成本与未来部署可替换性，选定。

| 维度 | 方案 A | 方案 B | 方案 C |
| --- | --- | --- | --- |
| Agent 认知成本 | 低 | 高 | 最低 |
| 内部 Interface depth | 低 | 中 | 高 |
| 状态 locality | 分散 | 集中 | 集中 |
| 当前迁移成本 | 低 | 极高 | 中 |
| 长任务支持 | 阻塞调用 | 原生异步 | 内部异步、外部语义 yield |
| 当前部署匹配度 | 一般 | 差 | 高 |
| v3 可演进性 | 差 | 高 | 高，可替换 Adapter |

## 4. 目标 Module 与 seam

```text
Agent / Agent SDK
        │
        │ observe(Query) / execute(Action)
        ▼
┌──────────────── Agent Gateway ────────────────┐
│ frame/input budget · decode · redact · project │
└───────────────────┬───────────────────────────┘
                    │ typed Query / RunCommand
                    ▼
┌────────────────── Runtime Core ─────────────────────────┐
│                                                         │
│ ObservationEngine        RunEngine                      │
│ selector + auto policy   sole transition authority      │
│         │                 │                              │
│         ▼                 ├─ WorkflowDefinitions        │
│ Evidence/ArtifactStore    ├─ Policy / Gate / Incident    │
│                           ├─ RunStore / Run ledger       │
│                           └─ DomainExecutor              │
│                                      │                  │
│                                      ▼                  │
│                              MutationJournal            │
│                              target lease / epoch        │
└─────────────────────────────────────────────────────────┘
                    ▲
                    │ governance Query/Command
             Operator / CI Interface
```

### 4.1 Gateway 到 Runtime 的最小 typed Interface

```python
class SemanticRuntimePort(Protocol):
    def observe(
        self,
        query: ObservationQuery,
        *,
        task_id: str,
        operation_id: str,
    ) -> ObservationResult: ...

    def execute(
        self,
        command: RunCommand,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn: ...
```

外部 MCP 与内部 typed seam 都使用 `observe` 和 `execute`，但 Gateway 进入 Runtime 前已将
JSON 解码为关闭的 typed 领域对象。

`RunCommand` 是关闭的 discriminated union：

```text
StartRun
SubmitGate
ResumeRun
ReconcileRun
CancelRun
```

Worker result、timer 和 operator resolution 是 RunEngine 的内部 command，不进入 Agent schema。

### 4.2 唯一状态转换 Interface

```python
class RunEngine:
    def execute(
        self,
        command: RunCommand,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn: ...
```

这一项单方法 Interface 应隐藏：definition pinning、事件加载与提交、幂等、Gate lifecycle、Effect 调度、Mutation reconcile、Outcome、Session Outcome 投影和 restart recovery。

核心不变量：

- 只有 `RunEngine` 可以提交改变 Run、Gate、Incident 或 Outcome 的事实；
- `DomainExecutor` 只能返回 typed `DomainResult`，不能直接推进 Run；
- `ArtifactStore` 只拥有内容、digest、ACL、retention 与 GC，不拥有控制流；
- `WorkflowDefinitions` 是纯定义 Module，不写运行状态；
- `MutationJournal` 是目标副作用事实源，Run ledger 只引用其 operation/effect identity 和投影状态；
- terminal Run 不得重新打开；
- 成功 Outcome 必须建立在所需 Acceptance 全部通过且所有相关 Mutation 已验证的基础上。

### 4.3 `WorkflowDefinitions` 的职责

`WorkflowKernel` 已更名为 `WorkflowDefinitions`，保留：

- route resolution；
- definition serialization、version、fingerprint；
- Run 创建时的 definition pinning；
- step identity 与 semantic cursor 计算；
- 老版本定义的兼容读取。

不应放入：

- 下一步选择；
- Gate 打开/关闭；
- Domain 调用；
- Event commit；
- Retry、reconcile 或 Outcome。

旧 `WorkflowKernel` 名称只作为迁移期 alias 保留，生产代码使用
`DEFAULT_WORKFLOW_DEFINITIONS`，避免形成两个“内核”权威。

### 4.4 `DomainExecutor` 的职责

建议 Interface：

```python
class DomainExecutor:
    def execute(self, command: DomainCommand, *, context: EffectContext) -> DomainResult: ...
    def reconcile(self, effect: EffectRef, *, context: RecoveryContext) -> ReconcileResult: ...
```

它隐藏 Adapter lookup、timeout、credential reference、target lease、effect identity、target epoch、MutationJournal 和 fresh verification。Domain Adapters 在 Runtime 构造时注册一次，不再逐调用传入。

只有生产 Adapter 与测试 fake 两种真实变化存在时才保留 seam；不要为每个内部函数制造单方法透传类。

## 5. 领域权威与术语

| 对象 | 权威 Module | 含义 |
| --- | --- | --- |
| `Run` | `RunEngine` / `RunStore` | 一次绑定 definition version、target identity/epoch 的执行 |
| `Case` | 当前作为 `run_id` 的存储兼容名称 | 暂不新增独立聚合；出现“一问题多 Run”需求后再引入 |
| `Step` | `WorkflowDefinitions` 定义，`RunEngine` 记录状态 | 版本化计划中的一个转换 |
| `Gate` | `RunEngine` | 预期的外部输入请求 |
| `GateSubmission` | `RunEngine` | 对明确 Gate 版本的一次不可变提交 |
| `Incident` | `RunEngine` | 自动推进无法安全继续的异常 |
| `EffectRecord` | `RunEngine` 投影 | 某个 Domain Effect 在流程中的身份与状态 |
| `MutationJournal` | `DomainExecutor` / Mutation Runtime | 真实目标副作用与验证的 durable truth |
| `ArtifactRef` | `ArtifactStore` | 内容的有界句柄、digest 与元数据 |
| `Turn` | Gateway 投影 | Agent 当前需要知道的有界视图，不是 source of truth |
| `ObservationReceipt` | Observation 投影 | 小型语义结果与 `observation_ref`，不是 Domain 执行记录 |
| `DomainResult` | `DomainExecutor` | typed Domain 执行结果 |
| `Outcome` | `RunEngine` | Run 的唯一终态事实 |
| `SessionOutcome` | 治理投影 | 从 terminal Outcome 自动生成，不再由 Gateway 形成第二次权威写入 |

Receipt 不再作为所有返回物的通用名称。`Receipt` 只保留给面向调用方的可验证投影；持久状态分别称为 Record、Submission、Event、Outcome 或 ArtifactRef。

## 6. `execute` 返回 Turn，而不是统一 CommandAck

### 决策

`execute(Action) -> Turn` 保持不变。Runtime 应推进到下一真实语义 yield：

- `Gate`：需要 Agent、人或外部系统提供明确输入；
- `Incident`：自动执行无法安全继续；
- `Outcome`：Run 终结；
- 可选 `running` Turn：调用期限内尚未到达语义 yield，提供稳定 `run_id` 与安全 reattach 信息。

Agent 不应通过 `observe` 查询 Run 状态，也不应进行无意义 polling。`observe` 只负责目标和环境的只读 observation。

### 原因

- 当前 [`ResultProjector.turn`](../openubmc-target-runtime/openubmc_target_runtime/agent_gateway.py#L772) 已经实现 next-Gate/terminal 投影。
- `workflow_advance` 已能在确定性步骤间连续推进，并在 phase、unknown Mutation、blocker 或 terminal 停止。
- Agent 可见 Ack 会引入 submit + N 次 poll，增加模型轮次、重复上下文和状态解释。
- 真正有意义的性能指标是 time-to-next-actionable-Turn，而不是 time-to-Ack。

### 长任务语义

内部实现可以是 await、long-poll、scheduler 或 queue。若调用断开：

- Run 按持久状态继续或安全停止；
- 使用同一 command identity 重试时返回原 Turn 或重新附着；
- `ResumeRun` 可在已知 `run_id` 上重新附着；
- 不把 transport disconnect 解释为 Domain Effect 未执行；
- 不向 Agent 暴露 worker lease、queue offset 或 poll token。

### 否决方案

- **所有 execute 只返回 Ack，最终用 observe 查询**：混淆 observation 与 orchestration，增加轮次，否决。
- **同步阻塞到任意长任务完成且无 reattach**：无法应对 caller timeout，否决。
- **另加 run_status 顶层 Agent 工具**：重新扩张工具面，否决。

## 7. Outbox/Inbox 与 Worker 分离

### v2/v2.1 裁决

v2/v2.1 不引入正式 Outbox/Inbox：

- Domain 调用当前发生在同一进程内；
- 没有“数据库提交成功、消息发布失败”的跨系统 dual-write；
- 没有 Worker result 经消息系统重复投递；
- `MutationJournal` 已保护 effect identity、effect-start boundary、unknown 与 reconcile；
- 新增 Outbox 会形成 Run ledger、Outbox、Inbox、MutationJournal 四套相邻状态，当前 leverage 不足。

当前先实现本地 event-backed `RunEngine`，使用 inline dispatcher。它是进程内 Module，不是独立部署服务。

### 必须引入 Outbox/Inbox 的触发条件

```text
完成前返回 CommandAck  ──► durable Outbox
WorkOrder 跨进程       ──► Outbox + Worker Inbox
Worker result 跨进程   ──► Inbox + idempotent result commit
跨主机 Mutation Worker ──► shared durable store + monotonic fencing
```

满足以下任一结构性条件时，Outbox 成为必需而不是可选优化：

1. Runtime 在工作完成前对调用方承诺“已 durable accepted”；
2. Domain WorkOrder 发送到另一进程、容器或主机；
3. accepted command 必须在调用进程死亡后自动执行，无需调用方重试；
4. 消息发送与 `EffectScheduled` 需要原子一致；
5. 多 Worker 竞争同一 work item。

Inbox 在 Worker result 可能重复或跨进程返回时同时引入。Outbox/Inbox 不能替代 MutationJournal：前者保证消息至少一次交付和去重，后者解决真实设备副作用是否已经发生。

### 建议遥测信号（需 ADR 确认）

Agent 调用层的异步 Ack 不设置预置等待天数。出现任一已测信号即可启动评估；累计至少
200 个 `execute` 样本后，再将比例和 P95 视为足以支持长期架构决策的稳定趋势：

- P95 accepted-to-Turn 超过最小支持 caller timeout 的 50%；
- 至少 1% accepted 调用在 Turn 前断开或超时；
- operation slot 利用率持续高于 75%，且 P95 queue delay 已明显影响 SLO；
- 出现明确的独立发布、权限隔离或 active-active 部署要求。

这些信号用于触发评估，不阻塞当前流程；200 个样本只限定趋势置信度，不是异步 Ack
评估或当前研发流程的准入门禁。这些数字也不是当前已测事实。没有遥测前不得以“未来可能扩容”为理由预建分布式控制平面。

## 8. Gate、Blocker 与 Incident

### 8.1 Gate

Gate 是预期等待，必须持久化以下身份：

```text
gate_id
run_id
gate_version
workflow_step_id
schema_id / schema_version / schema_digest
state: open | submitted | expired | cancelled
deadline
required_role_or_capability
submission_id / submission_digest
```

Agent-facing Turn 内嵌不超过 4 KiB 的关闭 schema，并携带 Gate ID、版本和 schema digest。
内部研发 Runtime 不使用 one-time secret token；这一点由 ADR-0004 修订 ADR-0003。

提交规则：

- 同一 `submission_id + digest` 重放返回原 Turn；
- 不同提交竞争同一 Gate 时只有一个成功；
- 错误 Run、错误 Gate、旧 version、不同 input digest 或 schema 不匹配返回 conflict；
- actor、提交时间与 transport identity 由 Adapter/Runtime 注入，不由模型自由声明；
- cancel 记录 `RunCancelled`，不伪造成一条 cancelled phase receipt。

### 8.2 Blocker

`blocked` 可以保留为 Turn 的通用投影状态，但不应继续承载所有持久语义。它可能来自输入缺失、策略拒绝、暂不可用或异常，无法支持稳定治理与统计。

### 8.3 Incident

Incident 表示非预期且自动推进无法安全继续，例如：

- unknown Mutation 无法由只读证据收敛；
- recovery/retry 耗尽；
- target identity、epoch 或 fence 不匹配；
- Artifact 缺失或 digest 错误；
- pinned workflow definition 不可加载；
- Run 不变量或投影一致性失败。

Incident 最小状态：

```text
open -> reconciling -> resolved
  └-----------------> operator_required
  └-----------------> cancelled
```

当前基线会先自动 reconcile unknown Mutation；仍无法收敛时返回显式 Incident Turn，且绝不
生成错误成功 Outcome。Incident Turn 已投影恢复路径、允许命令和 operator action；同一
unknown Effect 的重复 reconcile 复用既有 open Incident。Operator `runtime_status` 从持久
Run ledger 派生分类、open age、resolution、duplicate 和未知策略指标，SQLite 重启后保持
一致，不建立第二份状态权威。

## 9. 模型推理与确定性 Runtime 的分工

### 当前模型边界

模型继续在 Runtime 外负责：

- 假设生成与 Evidence 解释；
- selector、intent 和 delivery strategy 选择；
- 源码理解、设计、编辑与评审；
- Skill 路由、Agent handoff、用户澄清与最终说明。

Runtime 负责：

- typed input 验证与预算；
- definition pinning 与确定性状态转换；
- Gate/Incident/Outcome；
- policy、approval、idempotency 与并发控制；
- Domain Effect identity、lease、epoch、journal、verify 与 reconcile；
- Artifact 完整性与治理投影。

当前不增加 `ModelStep`。现有 developer/build phase Gate 已经准确表达“由 Runtime 外部能力完成复杂工作，再提交结构化结果”。

### 未来 Runtime-managed model call

只有需要无人值守、Runtime 自主调用模型时才增加 `ModelEffectAdapter`。它必须：

- 记录 request fingerprint、provider、model、版本、参数、prompt/template version；
- 输入只引用 Artifact/Evidence；
- 输出先持久化为 Artifact，再产生 `ModelEffectCompleted`；
- 记录 usage、latency、finish reason 和安全策略结果；
- 重启恢复复用已记录输出，不通过重新推理“重放”；
- 明确重试是否只增加成本，还是可能造成外部副作用；
- 不让模型输出直接越过 Policy/Gate 执行 Mutation。

Temporal 官方同样要求确定性 Workflow 与非确定性 Activity 分离，参见 [Workflow Definition](https://docs.temporal.io/workflow-definition) 与 [Activity Definition](https://docs.temporal.io/activity-definition)。openUBMC 借鉴的是责任分离，不是照搬 Temporal 的代码重放运行时。

## 10. Run、Effect 与恢复不变量

### 10.1 Run 状态

```text
created -> running -> waiting_gate -> running
                   -> incident     -> running | terminal
                   -> terminal
```

- Run 创建时固定 workflow definition ID、version 与 fingerprint；
- command identity 与规范化输入 digest 唯一绑定；
- terminal Run 永不复活；
- `Turn` 可以重建，不是权威状态；
- Session Outcome 从 terminal Run 事实投影，不允许独立改变 Run 结论。

### 10.2 Mutation 状态

沿用当前真实状态和 `effects_started` 边界：

```text
planned -> applying -> applied -> verifying -> verified
              │           │           ├-> verification_failed
              │           │           └-> verification_failed_terminal
              ├-> replan_required
              └-> mutation_failed (outcome unknown)

unknown/recovery
  -> replan_required
  -> verify -> verified | terminal failure
  -> rollback -> rollback_verified | rollback failure
  -> recovery_blocked/manual
```

关键不变量：

- `effects_started=false` 且只读检查证明未发生 Effect，才允许 replan；
- effect 可能开始后禁止使用新 identity 重试；
- recovery 必须使用相同 operation/effect ID 与输入 fingerprint；
- rollback 需要显式授权；
- fresh verification 必须使用新 target epoch；
- 无法证明最终状态时进入 manual/operator，不得生成成功 Outcome。

### 10.3 Run ledger 与 MutationJournal 的关系

两者是不同权威：

- Run ledger 记录“流程请求了哪个 Effect、当前能否继续”；
- MutationJournal 记录“真实目标副作用可能发生到哪里、如何验证或恢复”。

若进程在 MutationJournal 已 terminal、Run event 尚未提交时崩溃，恢复路径必须用同一 effect identity 再调用 DomainExecutor；后者从 terminal journal 返回 idempotent result，RunEngine 再补交 `EffectCompleted`。不得通过第二份状态猜测或直接跳过验证。

## 11. Live Patch 故障切点表

当前实现会在 root remount、backup、upload/replace 等首次潜在副作用前调用 `mark_effects_started`，见 [Live Patch apply](../openubmc-live-patch/openubmc_live_patch/runtime_backend.py#L836)。

| 崩溃切点 | durable truth | 恢复动作 | 明确禁止 |
| --- | --- | --- | --- |
| command/Run event 提交前 | 无已接受命令，无 journal | 使用同一 caller command identity 重试 | 假定命令已接受 |
| Run 已接受、journal 创建前 | Run 有待执行 Effect，无 target effect | 同一 effect identity 重新进入 DomainExecutor | 创建第二个 Effect identity |
| `planned` 后、`applying` 前 | journal 存在，`effects_started=false` | 只读检查；证明未执行后 replan 同一 identity | 删除 journal 后新建操作 |
| `applying` 且 `effects_started=false` | 尚未跨 effect boundary | 只读检查；收敛为 replan 或 manual | 无检查直接继续写目标 |
| root remount/backup 前后，响应丢失 | `effects_started=true`，结果未知 | 检查 mount mode、backup、目标 checksum；verify、rollback 或 manual | 盲目重复 remount/backup |
| upload/replace 已发出、响应前崩溃 | effect unknown | 检查 remote checksum、metadata、staging/backup 与 restart state | 重新上传并替换为新 operation |
| 文件替换成功、apply 尚未返回 | 目标可能已改变，journal 可能仍 applying/failed | 相同 identity reconcile；若 checksum 已匹配则进入 fresh verify | 仅因 apply 没返回就判定未执行 |
| `applied` 后、target epoch 前进前 | 已应用，尚未开始 fresh verify | 恢复/提升 epoch，直接 verify | 再次 apply |
| `verifying` 或 verification 响应丢失 | mutation 已应用，验证未确定 | 新 epoch 下重复只读 verify | 重复 Mutation |
| verify 证明终态失败 | `verification_failed_terminal` | 生成失败 Outcome 或显式授权 rollback | 将失败降级为 gap 后成功 |
| `verified` 后、Run event/Turn 前 | journal terminal verified | 同一 identity 返回 idempotent result，补交 Run event | 再执行目标操作 |
| Run terminal 后、Session Outcome 投影前 | Run Outcome 已是权威 | 重建治理投影 | 改写 Run 结论 |

必须覆盖的附加异常：root mount 恢复失败、backup 不完整、目标文件原本不存在、restart 响应丢失、target identity/epoch 变化。

## 12. Build-Upgrade 故障切点表

Upgrade 在 Redfish POST 前调用 `mark_effects_started`，见 [`_upload`](../openubmc-upgrade/openubmc_upgrade/runtime_backend.py#L543)；对不确定上传，现有 [`_recover_uncertain_upgrade`](../openubmc-upgrade/openubmc_upgrade/runtime_backend.py#L980) 会先读取 installed version 与 activation state，避免重新上传。

| 崩溃切点 | durable truth | 恢复动作 | 明确禁止 |
| --- | --- | --- | --- |
| source/build Gate 提交前 | Gate 仍 open | 同一 `submission_id` 重交或产生 conflict | 接受匿名重复 phase response |
| build Artifact 已生成、Gate commit 前 | Artifact 可能存在，Run 未接受 | 校验 ArtifactRef/digest 后幂等提交同一 Gate | 仅凭本地路径猜测产物身份 |
| Artifact digest/版本校验前 | 无 Upgrade effect | 拒绝不完整或不匹配 Artifact | 创建 mutation journal 后再校验基本身份 |
| journal `planned`/`applying` 且 effect 未开始 | 已接受 Upgrade，无 POST | 只读 inspection；replan 同一 identity | 更换 operation ID |
| `mark_effects_started` 后、POST 响应前 | 上传结果未知 | 查询 UpdateService、software inventory、版本和 activation | 立即重新 POST |
| HTTP 4xx 明确拒绝 | effect 被明确拒绝，可证明未接受 | 清除 effect-start，进入 `replan_required` | 标为 unknown 并永久阻断 |
| upload accepted、task URI 未持久化或响应丢失 | Artifact 可能已上传 | 只读 recovery inspection；相同 effect ID 收敛 | 重传同一固件包 |
| task monitor 连接丢失 | 可能 pending、完成或失败 | 查询当前版本和 activation，继续 verify | 仅按连接异常判定失败或重传 |
| Artifact available，但 inactive 且无 pending | activation fallback 终态 | `verification_failed_terminal`，失败 Outcome | 再上传来掩盖 activation 问题 |
| pending activation | 上传已生效到待激活阶段 | 等待/验证，不再上传 | 新建第二个 Upgrade |
| installed version 已变化、verification 未提交 | 目标很可能完成升级 | 新 target epoch 下 fresh verify | 重复上传或激活 |
| fresh verify 失败但非终态 | mutation 已应用、结果待收敛 | 保留 `verification_failed` 并重试只读验证 | 回到 apply |
| journal `verified` 后、Run event 前 | Upgrade 已验证 | journal idempotent replay，补交 Effect/Run event | 重复 Upgrade |
| terminal Run 后、Outcome 投影前 | Run Outcome 已确定 | 重建 Session Outcome/Turn | 形成第二事实源 |

真实或仿真测试必须覆盖 Redfish task URI 丢失、monitor connection lost、BMC reboot、旧版本回退、activation fallback、artifact digest 篡改和 target identity 变化。

## 13. 迁移路线

### P0：v2 发布收口（若 v2.0.0 已冻结则作为 v2.0.1 硬化）

目标：不改变产品方向，封住当前已知安全与预算缺口。

1. 为 `execute` 设置整体 serialized input budget，并限制字符串、数组、对象深度、属性数和错误响应。
2. 为 MCP stdio 增加解析前 frame/line byte limit。
3. `StartRun` 改传 `observation_ref + digest`，Runtime 重建 Receipt；兼容期可读完整 Receipt，但默认不再回传。
4. 引入持久 `gate_id + gate_version + schema digest + submission identity + input digest`，
   关闭 Gate schema；内部研发不使用 one-time secret token。
5. 明确 Effect/Mutation 的 at-least-once、idempotency、epoch/fencing 与 reconcile 表述，删除外部副作用 exactly-once 暗示。
6. 按上述切点完成 source-only、live-patch、build-upgrade 的重启和 fault injection。
7. 将正式 A/B 摘要与可复核 digest 纳入仓库。
8. 落盘核心 ADR；冻结新增 Agent-facing selector、Action kind 和工具。

退出条件：所有 P0 budget、Gate、Mutation 和 crash-cut 测试通过；默认工具仍为两个；unknown 不会生成成功 Outcome。

### P1：Runtime 内部深化

目标：让外部两个 operations 背后形成真正的 deep Modules。

1. typed `ObservationQuery/Result`、`RunCommand/RunTurn` 和两方法
   `SemanticRuntimePort`：已完成。
2. Gateway 删除旧 8 方法协调并切到 typed seam：已完成。
3. `WorkflowKernel` 改为 `WorkflowDefinitions`：已完成，保留兼容 alias。
4. 建立唯一 `RunEngine.execute` 并保持现有 event/storage 兼容读取：已完成主路径。
5. observation persistence、Gate 决策、Run Outcome 和 Session Outcome 投影移入 Runtime：
   已完成；Agent Gate persistence 使用原生 `RunGateSubmitted`。
6. Domain Adapters 构造时注册，`DomainExecutor` 统一执行策略：已完成基线。
7. Gate、Submission、Incident 和 Outcome 已有版本化持久事实；原生 Gate event writer、
   old-event upcaster、Incident 恢复策略和 Operator 生命周期指标已完成基线。
8. 行为 contract tests 和 fake Adapters：已补主路径，旧源码 contraction tests 逐步退役。
9. 继续按变化原因收缩 `context_runtime.py` 与 `mcp.py`，不以文件行数为单独目标。

退出条件：Gateway 只依赖两方法 typed Interface；RunEngine 是唯一状态写入者；核心测试不穿透 Module Interface。

### P2：v2.x 按遥测扩展

1. 仅根据真实缺口增加 D-Bus、active alarm、bounded log search selectors。
2. 建立 Artifact ACL、retention、GC、redaction 与 schema migration。
3. 基于 Incident 分类、open age 和 resolution 指标建立运行 SLO 与处置手册。
4. 建立 workflow definition pinning、old-run support、migration 与 deprecation policy。
5. 对只读 selector 做并行与连接复用，验证时间一致性。
6. 对较大 Skill 使用渐进披露，主文件只保留触发、所有权、主流程和安全规则。
7. 建立 execute token、time-to-actionable-Turn、unknown/reconcile、Artifact bytes 和 Gate wait 遥测。

### P3：v3 的条件式分布式演进

只有出现跨进程 Worker、active-active、独立发布/权限隔离或可量化容量瓶颈时才进入：

1. local async Ack + SQLite Outbox；
2. 先分离 read-only Worker；
3. 引入 Worker Inbox 与结果去重；
4. 共享 durable store、Artifact store 与 monotonic fencing；
5. 最后迁移 Mutation Worker；
6. 再评估 Temporal 或其他 durable backend 作为 Adapter。

即使 v3 更换基础设施，Agent-facing `observe/execute`、Run/Gate/Incident/Effect/Artifact 领域语义仍保持稳定。

## 14. 测试与 Benchmark

### 14.1 Interface contract

- 默认 tools/list 只含两个 operations，schema 总量有界；
- JSON 只存在于 MCP/CLI Adapter，Runtime tests 使用 typed command；
- 相同 command identity + digest 返回原 Turn；不同 digest conflict；
- Gate 同提交幂等、并发提交单赢家、旧 version/错误 Gate/不同 input digest conflict；
- terminal Run 不复活，Session Outcome 可从 Run 重建；
- ArtifactRef digest、ACL、target/run binding 和 GC 后行为 fail closed。

### 14.2 状态与性质测试

- 随机 RunCommand/Event 序列保持不变量；
- 每个持久化 commit 前后 kill/restart；
- Run projection 可由 event-backed facts 重建；
- 老 definition/event/schema 可兼容读取或明确拒绝，不能静默改义；
- 任一 Incident 未解决时不能产生 success Outcome。

### 14.3 Mutation fault injection

- Live Patch 与 Upgrade 覆盖第 11、12 节全部切点；
- 重复 delivery、连接断开、响应丢失、进程 kill、target epoch 变化；
- 断言 effect identity 恒定、危险 Effect 不重复、unknown 必须 reconcile；
- `verified` journal 与 Run event 之间的 crash 必须通过 idempotent replay 收敛。

### 14.4 Runtime stability qualification

当前 CI qualification 通过公开 Agent execute seam 与持久 repository 运行 hermetic duplicate
storm、SQLite Gate 并发单赢家、独立 128 Run capacity、64 Run restart soak 和既有 crash-cut
matrix。报告绑定 source commit、环境指纹、参数、Agent 调用/失败/完成数、逐 batch/cycle 事件与
存储增长、进程峰值 RSS、Python allocation、耗时和 digest；聚合资格会
重新验证 schema、source、参数、digest 和全部硬阈值。重复 Outcome、未收敛
Incident/operation、同 identity 接受不同输入或增长超限都会阻断 promotion。并发 storm
允许返回 `running` reattach Turn，但必须用同一 command identity 收敛到唯一 terminal
Outcome；Gate 并发要求所有等价 caller 返回语义相同的 terminal Turn；soak replay 必须发生在
Runtime reopen 之后且不得再次调用 backend。source 绑定仅接受当前 HEAD，或合法 release-lock
child 记录的唯一父提交。

### 14.5 Execute A/B

使用现有配对 AB/BA 方法，固定模型、prompt、target snapshot 与 commit：

| 场景 | Turn 方案期望 Agent 调用数 |
| --- | ---: |
| diagnosis-only 到 Outcome | 1 |
| source-only：start → change Gate → Outcome | 2 |
| build-upgrade：两个 Gate → Outcome | 3 |
| live-patch unknown → reconcile → Outcome/Incident | 3 |
| 0.2、5、30、120 秒内部 Effect | 不增加模型调用 |
| 每个 durable cut point 重启 | 一次 reattach，无重复 Effect |
| full Receipt 对 ArtifactRef start | 语义相同，后者 Token 显著更低 |

指标：

- Agent model turns；
- tool calls；
- total tokens；
- non-cached input + output；
- tool-output bytes；
- time-to-next-actionable-Turn；
- reattach 次数；
- duplicate Effect 数；
- unknown 到最终 disposition 的时间；
- false-success 数。

性能目标沿用现有资格方法：10 对为首个决策点，不确定扩展到 20、30 对；正常同质量路径目标不高于旧基线 `1.10×`，单侧置信区间硬上限 `1.15×`。安全门禁始终是 duplicate dangerous Effect = 0、scope violation = 0、unsupported claim = 0、false success = 0。

## 15. 必须落盘的 ADR

1. 一个 Runtime Core、两个 Agent operations、独立 Operator/CI Plane；
2. Gateway JSON Adapter 与 typed Runtime seam；
3. `WorkflowDefinitions + RunEngine` 及唯一状态权威；
4. `execute -> Turn` 与内部异步/reattach 语义；
5. Effect at-least-once、idempotency、target epoch/fencing、unknown reconcile；
6. Gate version、schema digest、Submission 幂等与冲突检测；
7. ArtifactRef/ObservationRef 与大对象 claim-check；
8. 局部 event-backed ledger，不全面 Event Sourcing/CQRS；
9. Incident 与 Gate/Blocker 的语义区分；
10. Outbox/Inbox、Worker 和 shared backend 的采用触发条件；
11. model invocation 作为未来非确定性 Effect；
12. compatibility profile 的退役门槛。

## 16. 明确否决或延后

| 方向 | 裁决 | 原因 |
| --- | --- | --- |
| 恢复 19 个 Agent-facing 工具 | 否决 | 再次泄漏 Runtime sequencing |
| 内部继续使用万能 `Mapping[str, object]` | 否决 | 类型、不变量和版本分散 |
| 同时存在 WorkflowDefinitions 与 RunEngine 两个状态机 | 否决 | 定义 Module 不能成为第二状态权威 |
| Agent 可见统一 CommandAck + polling | 当前否决 | 更多轮次和调度概念，无规模证据 |
| 立即引入 Outbox/Inbox/Broker | 延后到 v3 触发 | 当前无跨进程 dual-write |
| 完整 Event Sourcing/CQRS | 否决 | 迁移和版本成本高于当前收益 |
| BPMN/通用 DAG 作为核心语言 | 否决 | 动态技术工作流不匹配 |
| Runtime 内立即增加 ModelStep | 否决 | 当前 Agent/phase seam 已足够，增加非确定性 |
| 动态 Agent Action Catalog | 延后 | 当前只有少量固定 Run commands，问题是输入无界而非 Action 数量 |
| 自动补偿所有 Mutation | 否决 | 只有可证明可逆且获授权的 Effect 才能 rollback |
| 仅为降低行数拆文件 | 否决 | 应按状态权威和变化原因建立 locality |

## 17. 最终方向判断

当前方向和第一轮内部收敛都已验证正确，下一步仍不应扩张 Agent Interface：

- 保留 Runtime Core、`observe/execute`、MutationJournal、Evidence/Artifact、Replay 与治理面；
- `WorkflowDefinitions` 已校正为纯定义 Module；
- typed `SemanticRuntimePort` 与 `RunEngine` 已接管 Gateway 的 8 方法协调、自动 reconcile
  和 Session Outcome 投影；
- M4 主路径权威收敛已完成；后续用 compatibility telemetry 驱动旧 writer 退役；
- 保留 Turn-to-next-Gate，隐藏 Ack、polling、scheduler 与未来 Worker；
- v2 做本地 typed event-backed Process Manager，不做分布式平台；
- 继续用 Incident 运行数据驱动 SLO，并推进 compatibility writer 退役、长时 soak、property
  和 network fault injection；
- 只有规模与部署证据出现后，才把 Outbox/Inbox、Worker、共享 fencing 与 durable backend 作为一个完整 v3 演进包引入。

这条路线具有最高 Interface depth：Agent 学习的概念最少，Runtime 提供的行为最多；同时把状态、安全与恢复规则集中到一个可测试 seam，获得更高 leverage 和 locality。
