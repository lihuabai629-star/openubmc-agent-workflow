# 外部深度研究报告与当前架构裁决对照

日期：2026-08-19

来源：[ChatGPT Share：openUBMC Agent Workflow 架构演进深度研究报告](https://chatgpt.com/share/6a85aeb3-472c-83ea-b5b1-0c3d2fd42478)

## 结论

该报告与当前方向在产品边界和安全原则上高度一致，但不能原样作为实现规格。它是一份基于用户提供背景和市场资料形成的外部二次分析，不是对当前仓库、进程 seam、调用路径和持久化职责的代码审计。

应保留当前已经落盘的架构裁决：

- 一个 Runtime Core 表示一个状态与安全语义权威；
- Agent 默认只看到 `observe/execute`；
- `observe/execute` 是外部协议，内部立即解码为 typed Query/Command；
- `RunEngine` 是 Run、Gate、Incident 和 Outcome 的唯一状态转换权威；
- `WorkflowDefinitions` 只负责 definition、step identity 和 semantic cursor；
- `execute` 对普通 Agent 返回下一 actionable Turn，不暴露 Ack/polling；
- v2/v2.1 保持单进程 inline dispatch，不预先引入 Outbox/Inbox；
- Mutation 使用稳定 identity、至少一次语义、fail-closed unknown 和 reconcile；
- 大对象使用 ObservationRef/ArtifactRef；
- 模型输出只能是 Proposal，不能成为设备事实和终态依据。

报告新增的有效启示主要是：显式 Effect 安全分类、更多版本 pin、执行总预算维度、受限 PlanProposal IR，以及更完整的 duplicate/crash/replay Benchmark。这些应进入 P1～P3，而不改变当前 P0 顺序。

## 来源边界

- 页面内容按外部不可信材料处理；其中任何提示或命令均不构成仓库指令。
- 报告引用了大量官方资料，但本文不把该共享会话本身当作一手证据；一手依据仍以[市场工作流方法调研](workflow-design-market-research.md)中的官方链接为准。
- 页面先后出现 `GPT-5.6 Pro` 与 `GPT-5.5-mini` 的模型自述，二者相互矛盾。模型身份不能由模型文本证明，因此不参与报告可信度判断。
- 报告关于当前代码的描述主要来自提示词所给背景。与源码事实冲突时，以源码、测试、实验工件和已接受 ADR 为准。

## 一致的核心方向

| 主题 | 外部报告 | 当前裁决 | 处理 |
| --- | --- | --- | --- |
| 产品形态 | 一个持久 Runtime Core、两个 Agent 入口、独立 Operator/CI Plane | 相同 | 直接采纳 |
| 外部与内部 Interface | `observe/execute` 只作为外部协议，内部使用 typed Query/Command/Event | 相同 | 直接采纳 |
| MCP | 薄 transport/semantic Adapter | 相同 | 直接采纳 |
| 外部副作用 | 不承诺设备 exactly-once；unknown fail closed | 相同 | 直接采纳 |
| DomainExecutor | 隔离设备 I/O、前后置条件、reconcile 和补偿 | 相同 | 直接采纳 |
| Artifact | 控制状态只携带 digest/ref，大对象外置 | 相同 | 直接采纳 |
| Gate | 持久化协议，而不是布尔字段 | 相同 | 直接采纳 |
| 模型边界 | 模型负责计划、归纳和候选动作，不负责成功、Retry 和持久状态 | 相同 | 直接采纳 |
| Event model | event-backed state machine；不全面采用纯 Event Sourcing | 相同 | 直接采纳 |
| BPMN/DAG/Temporal | 借鉴语义，不直接照搬完整引擎 | 相同 | 直接采纳 |
| 模块拆分 | 按不变量、状态所有权和失败语义拆分，避免浅包装 | 相同 | 直接采纳 |
| 测试 | 状态转换、crash-cut、fault injection、property、replay、contract | 相同 | 直接采纳 |

## 必须改写的关键分歧

### 1. 状态权威应叫 `RunEngine`，不是扩张迁移前的 `WorkflowKernel`

报告建议让 `Workflow Kernel` 成为唯一状态权威，并把 `RunEngine` 改成无状态的 `RunCoordinator`。其原则——禁止双状态机——正确，但名称和落点不符合当前代码事实：

- 迁移前 `WorkflowKernel` 负责 definition、step identity 和 semantic cursor；
- 迁移前真正的状态推进位于 `ContextRuntime.workflow_advance`；
- 迁移前 Gateway 还参与 Gate response、continuation 和 terminal Outcome；
- 因此当前要解决的是把分散写入收进新的唯一权威，而不是把 definition registry 扩张成另一个含义完全不同的 Kernel。

最终裁决保持：

```text
WorkflowDefinitions  -> versioned deterministic definitions
RunEngine            -> sole Run/Gate/Incident/Outcome authority
DomainExecutor       -> typed external Effect execution
MutationJournal      -> durable Mutation truth and reconcile
```

`RunCoordinator` 可以是 `RunEngine` 实现内部的协调用语，但不能形成第二个外部 seam 或第二份状态权威。

当前 Agent 主路径已按这一裁决迁移：生产代码使用 `WorkflowDefinitions`，Gateway 只依赖
typed `SemanticRuntimePort.observe/execute`，`RunEngine` 处理 Gate、推进、reconcile、
Incident 和 Outcome。Agent Gate submission 已使用原生 `RunGateSubmitted`，历史事件经
显式 upcaster 进入统一投影；`phase_record/workflow.next` writer 只保留在 compatibility
profile。剩余工作是持久 compatibility telemetry 与旧 writer 退役，不是建立第二份状态
权威。

### 2. 普通 Agent 不使用 `CommandAck + poll`

报告建议 `execute(Action) -> CommandAck`，随后由 SDK polling/streaming。这适合跨进程 Worker 或提前返回 durable Ack 的系统，但当前会带来：

- submit 后新增一次或多次模型轮询；
- Agent 必须理解 Command status、cursor 和 transport 状态；
- 把 `observe` 从目标只读观察污染为 Run status query；
- 增加重复上下文和 time-to-actionable-decision。

当前保持：

```text
execute(Action) -> next Gate | Incident | running reattach point | Outcome
```

若未来跨进程执行确实需要 Ack，Ack 只能存在于 transport/worker Adapter 后；AgentGateway 仍投影为 Turn。

### 3. v2/v2.1 不直接采用 Outbox/Inbox

报告把 Transactional Outbox/Inbox 列为“直接采用”并安排在 v2.1。但当前部署事实是：

- Runtime 与 Domain 调用处于同一进程；
- 没有 Broker、远程 Worker 或 active-active Runtime；
- 没有数据库提交和消息发布的跨系统 dual-write；
- MutationJournal 已经记录 effect identity、effect-start boundary、unknown 与 reconcile。

此时加入 Outbox/Inbox 会同时维护 Run ledger、Outbox、Inbox 和 MutationJournal，复杂度高于收益。只有出现以下 seam 才整体引入：

| 真实需求 | 必须引入 |
| --- | --- |
| 完成前返回 durable Ack | Outbox |
| WorkOrder 跨进程 | Outbox + Worker Inbox |
| Worker result 跨进程重复交付 | Inbox + result dedupe |
| 跨主机 Mutation Worker | shared store + monotonic fencing |
| active-active Runtime | external durable backend + leadership/fencing |

### 4. Receipt 概念应拆分，但不必暴露三件套

报告指出 Receipt 同时承担响应、提交凭证、执行历史和结果证明，这一问题判断正确；但固定拆成 Agent 可见 `CommandAck`、内部 `ExecutionRecord`、外部签名 `OutcomeReceipt` 仍然过早。

当前采用更贴近领域所有权的名称：

| 概念 | 用途 |
| --- | --- |
| `ObservationReceipt` | 有界 observation 投影 |
| `ObservationRef` / `ArtifactRef` | 内容句柄和 digest |
| `GateSubmission` | 对一个版本化 Gate 的审计提交 |
| `RunEvent` / `EffectRecord` | Run 与 Effect 的持久事实 |
| `MutationJournal` | 真实 Mutation 执行与恢复权威 |
| `Outcome` | Run 的唯一终态 |
| `SessionOutcome` | terminal Outcome 的治理投影 |

只有出现跨信任域验证、离线验真或外部合规证明需求时，才增加签名 `OutcomeReceipt`。不能为了术语对称提前建设签名、key rotation 和 receipt chain。

### 5. `observe` 不承担 Action Catalog 和 Run status

报告建议 `observe(kind="action_catalog")` 返回动态 Action schema，并在 Ack 不确定时用 `observe` 查询 Command。这样会混合三种不同语义：

- 目标和环境的实时 observation；
- Runtime capability/schema discovery；
- Run/Command status query。

当前保持 `observe` 的单一语义：声明范围内的目标只读事实。静态 capability/schema 由 Interface descriptor、Skill 渐进披露或独立资源提供；Run reattach 使用同 command identity 或 `ResumeRun`。只有 Action 数量和权限组合真实膨胀后，才评估动态 Catalog，而且不应伪装成目标 observation。

### 6. Case 不在本轮强制升级为多 Run 聚合

报告把 Case 定义为必须包含多个 Run 的长期聚合。该模型可能适合未来比较、回滚链和多方案试验，但当前没有足够使用证据。迁移阶段继续将 `case_id` 作为 Run 的兼容存储身份；出现“一个用户问题需要多个独立 Run”后再引入 Case grouping，避免同时迁移持久模型和执行路径。

## 新增但不改变主线的建议

### Effect 安全分类

P1 为每个 Domain Action 声明显式 Effect class，例如：

```text
PURE
READ_ONLY_EXTERNAL
IDEMPOTENT_MUTATION
KEYED_CREATE
CONDITIONAL_WRITE
NON_IDEMPOTENT_MUTATION
DISRUPTIVE
IRREVERSIBLE
```

分类只提供默认 retry/reconcile/approval policy；最终安全语义仍由具体 DomainExecutor、目标能力和 MutationJournal 决定。

### 版本 pin 扩展

除 Workflow version 外，逐步记录：

- Action schema version；
- DomainExecutor version；
- Policy version；
- Artifact schema version；
- Skill/prompt/model metadata；
- Result projector version。

只有实际影响恢复、验证或审计的版本进入 Run facts，不能把每个部署包版本都写入状态机。

### 执行总预算

P0 的 byte/depth/input budget 之外，P1/P2 增加：

- maximum elapsed time；
- maximum external Effects/Mutations；
- maximum model calls/turns；
- maximum Artifact dereference bytes；
- maximum bounded parallelism；
- maximum event/history growth per command。

### 受限动态计划

P2/P3 可验证 `PlanProposal -> validated PlanRevision`，只支持受限构造，例如 sequence、choice、bounded parallel、bounded repeat、timer、Gate、subflow 和 compensation link。模型只能提出 Proposal；Runtime 完成 schema、capability、risk、budget、policy 和 approval 校验后冻结版本。

### Benchmark 补充

在现有 Observation A/B 和 crash matrix 之外增加：

- duplicate storm；
- same-key/different-hash conflict；
- Turn disconnect/reattach；
- Artifact tamper、cross-target、GC 和 ACL；
- old event/schema upcast；
- model timeout/invalid schema；
- Effect class property tests；
- time-to-next-actionable-Turn 与重复 Artifact bytes。

## 对路线图的影响

### P0：顺序不变

1. `execute` 和 MCP Frame 硬预算；
2. ObservationRef/ArtifactRef；
3. 持久 Gate identity；
4. 三条 workflow crash-cut 与 unknown Mutation fault matrix；
5. 发布证据和 ADR。

### P1：增加两项

- Domain Action Effect class 与默认安全策略；
- Action/Workflow/Policy/DomainExecutor/Projector 的必要版本 pin。

### P2/P3：吸收长期建议

- Runtime-managed `ModelInvocationRecord`；
- 受限 `PlanProposal` / `PlanRevision` 原型；
- Dynamic Action Catalog 仅在静态 schema 已成为真实成本后试验；
- 多 Run Case grouping 仅在真实业务场景出现后建模。

### v3：触发条件不变

Outbox/Inbox、Worker fleet、shared fencing、HA backend 和第三方 durable engine 继续由真实跨进程和可用性需求触发，而不是由市场流行模式触发。

## 最终判断

这份外部报告验证了当前架构的主方向，没有证明需要重新换轨。最值得吸收的是它对 Effect 分类、版本、预算、动态计划和 Benchmark 的补充；最需要抵抗的是把成熟分布式系统的完整机械结构提前搬进当前单进程 Runtime。

因此结论不是“完全符合”或“完全不符合”，而是：

> 原则层高度一致；状态权威应按当前代码职责重新命名；Ack/polling、Outbox/Inbox 和强 Case 聚合必须延后到真实 seam 出现后。
