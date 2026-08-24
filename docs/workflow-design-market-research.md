# openUBMC Agent Workflow：市场工作流方法调研与演进判断

日期：2026-08-19

评估基线：`refactor/agent-semantic-gateway`，`db38a4a`

实现更新：Issue #25 的首轮收敛已在 `refactor/run-engine-core` 落地，包括 typed
`SemanticRuntimePort`、`RunEngine`、`WorkflowDefinitions`、预注册 `DomainExecutor`、
ObservationRef/ArtifactRef、持久 Gate identity、自动 reconcile、Incident 和 256 KiB
request/frame 预算。本文的“当前实现基线”和缺口表保留为调研时快照；后续状态以
[演进档案](workflow-evolution-roadmap.md)、[路线图完成审计](roadmap-completion-audit.md)和
ADR-0004 为准。

## 执行摘要

当前架构方向是正确的，但只完成了“入口收缩”，尚未完成“执行内核深化”。应继续保留一个持久化 Runtime Core，将 `observe(Query)` 与 `execute(Action)` 作为默认 Agent Interface，并把 Operator/CI 留在独立治理面；下一阶段不应重新增加 Agent-facing 工具，也不应立即替换为 Temporal、BPMN、LangGraph 或通用 DAG 引擎。

市场上的成熟工作流系统虽然表面模型不同，但在几个关键问题上高度一致：协调逻辑与外部副作用分离；外部执行按至少一次交付设计并要求幂等；人机等待是持久化外部事件；大数据通过 Artifact 引用流转；工作流版本、历史增长和重放兼容必须显式治理。openUBMC 应吸收这些语义，而不是照搬这些产品的编程模型或部署形态。

正式 10 对 A/B 已证明新的 `observe` 入口有效：总 Token 几何均值比为 `0.234815`，非缓存输入加输出为 `0.320367`，耗时为 `0.371208`，三项均通过门禁。但这只能证明 Observation 收缩有效，不能证明完整的 `execute`、重启恢复和未知副作用已达到发布条件。

近期最重要的工作不是引入新编排框架，而是完成以下闭环：

1. 对 `execute` 输入和 MCP stdio 帧设置硬上限；
2. 用 `handle + digest + metadata` 代替完整 ObservationReceipt 回传；
3. 为 Gate 引入版本化、一次性的提交身份；
4. 明确副作用采用“至少一次执行 + 幂等 + fencing/target epoch + reconcile”，不承诺外部副作用 exactly-once；
5. 将当前 JSON 透传式内部接口重构为 `ObservationEngine`、`RunEngine`、`DomainExecutor` 等 typed deep Modules；
6. 对三条交付路径补齐正常、进程重启、故障注入三类 `execute` 资格验证。

## 关键发现

1. **正确的产品 Interface 已经出现。** 两个语义入口比 19 个 Runtime 概念型工具更符合 Agent 的认知单位，也显著降低了 Observation 成本。
2. **MCP 只是 Adapter，不应成为领域模型。** JSON Schema、工具数量和传输协议可以变化，Run、Gate、Mutation、Evidence 的语义不应随传输层变化。
3. **真正的风险在 Mutation，不在流程跳转。** 工作流状态可以重放，已经发往 BMC 的升级、文件替换或控制命令不能假设可安全重放。
4. **“exactly-once workflow execution”不能外推成“exactly-once external effect”。** openUBMC 应公开采用至少一次副作用模型，并通过持久 effect identity、幂等、目标 epoch/fencing 与 reconcile 收敛未知状态。
5. **Gate 需要成为持久领域对象。** `respond` 不应只是给当前阶段提交一个自由 JSON，而应提交给明确的 `gate_id + gate_version`，并拒绝重复、过期或跨 Run 的响应。
6. **Artifact 是状态规模控制的基础设施。** 原始 Evidence、日志包、构建产物和较大结果应存入内容寻址存储，Agent 与流程状态只携带句柄、摘要和必要元数据。
7. **调研时内部 seam 仍偏浅。** 当时 `AgentGatewayRuntimePort` 暴露 8 个方法；该缺口已由
   typed `SemanticRuntimePort + RunEngine + DomainExecutor` 主路径收敛。
8. **不应提前平台化。** 在单机 Runtime 的吞吐、可用性或多租户瓶颈得到证据前，不应预先引入 HA 队列、BPMN、Temporal 或完整 Event Sourcing/CQRS。

## 研究方法与证据等级

本报告优先使用官方文档、当前源码和实际实验工件。结论按以下等级区分：

- **事实**：可由官方文档、源码或实验结果直接验证；
- **架构推论**：由多个事实推导出的 openUBMC 设计判断；
- **待验证假设**：需要原型、故障注入或基准测试确认，不能作为既成能力宣传。

## 调研时实现基线（历史快照）

### 已验证事实

- 默认 Agent profile 只暴露 `observe` 和 `execute`；兼容面与 Operator 面显式分离。
- Observation scope 上限为 2 KiB，ObservationReceipt 上限为 4 KiB，Turn 上限为 8 KiB，Gate schema 上限为 4 KiB。
- `execute` 支持 `start | respond | resume | control`，并把内部推进上限固定为 64 步。
- `execute` 的 JSON Schema 对 `intent`、`purpose`、`run_id`、`target` 等字符串没有统一长度上限，`workflow`、`response` 与完整 `observation_receipt` 也缺少总字节预算。
- MCP stdio server 在 `json.loads` 前逐行读取输入，但没有绝对帧长或行长上限。
- 评估时 `AgentGatewayRuntimePort` 暴露 8 个内部方法；`RuntimeSDK.execute` 仍要求调用方逐次传入 Domain Adapter。
- 核心文件已经明显膨胀：`context_runtime.py` 6551 行、`mcp.py` 4258 行、`agent_gateway.py` 1174 行。
- `test_contraction_contracts.py` 包含源码字符串与函数体切片断言，重构时容易产生非行为性失败。
- 11 个 Skill 入口文件合计约 109 KiB，其中 `openubmc-debug/SKILL.md` 约 18 KiB；渐进披露仍有优化空间。
- 从 `v1.0.0` 到当前 HEAD 共变化 69 个文件，新增 14511 行、删除 931 行，说明 v2 已经是一次显著的架构扩张，而不是轻量接口调整。

### 实验事实

| 实验 | 结果 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| Agent Gateway 正式 10 对 A/B | 总 Token 比 `0.234815`；有效 Token 比 `0.320367`；耗时比 `0.371208`；通过 | `observe` 的语义收缩能显著降低 Agent 成本与耗时 | 完整 `execute`、Mutation 恢复、Gate 重放和生产可用性 |
| 早期复杂诊断 MCP 对 CLI | 总 Token `+128.50%`；有效 Token `+27.38%`；耗时 `+49.42%`；证据质量约从 `8.5/10` 到 `10/10` | Runtime 资产、Evidence 完整性与可审计性有价值 | 把内部 Runtime 概念直接暴露给 Agent 是高效 seam |
| 定向单元测试 | 55 tests，全部通过 | 当前语义入口与选定发布门禁未回归 | 真实 BMC、长流程、崩溃切点和重复副作用均已覆盖 |

### 架构推论

- Runtime Core 的资产值得保留；失败的是旧的 Agent-facing seam，而不是持久化 Runtime 的价值主张。
- 入口收缩已经解决“Agent 必须理解多少内部概念”的问题，下一阶段应解决“Runtime 如何可靠承担复杂性”的问题。
- 如果内部继续用宽泛 `Mapping[str, object]` 和大量透传方法，外部两个工具最终仍会退化成“两个万能 JSON 工具”，只是把原来的 19 个名字隐藏起来。

## 市场方法统一比较

| 设计流派 | 代表系统 | 核心执行单位 | 持久化/恢复模型 | 外部副作用语义 | 人机等待 | 大数据处理 | 对 openUBMC 的价值 | 不应照搬 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Durable execution | Temporal、AWS Step Functions、Azure Durable Functions | Workflow/State/Orchestrator + Activity/Task | 历史、状态机或事件重放 | Activity/Worker 可重试，调用方必须处理幂等 | Callback token 或 external event + timer | 有状态/历史上限，建议外置大载荷 | 协调与副作用分离、版本化、恢复语义 | 现在就引入完整 replay VM 或外部控制平面 |
| Human process/BPMN | Camunda 8 | Process instance、Job、User Task | Broker 持久状态与 Job lease | 至少一次 Job delivery，Worker 幂等 | User Task 有显式生命周期、分配和表单 | 变量与业务数据分层 | Gate 生命周期、incident、人工处置语义 | 用 BPMN 描述所有动态诊断与技术工作流 |
| DAG/data workflow | Argo、Airflow、Dagster、Prefect | Step/Task/Asset | DAG run、task state、cache/result | 任务重试与缓存，通常要求幂等 | 一般不是核心强项 | Artifact/Object storage/Asset identity | Artifact 句柄、依赖身份、缓存与 GC | 把 Agent 驱动的分支循环压成静态 DAG |
| Agent orchestration | LangGraph、OpenAI Agents SDK、Google ADK、Microsoft Agent Framework | Node/Agent/Run/Thread | Checkpoint、session、resume | 中断前副作用仍需幂等；应用拥有工具和存储 | Interrupt、approval、HITL | 独立 Artifact service 或应用自管 | Agent UX、暂停恢复、handoff、trace/eval | 把 Agent checkpoint 当作权威 Mutation ledger |
| Distributed consistency patterns | Saga、Outbox、Event Sourcing、CQRS | Command/Event/Transaction | 日志、补偿、投影 | 重复交付与补偿失败必须显式处理 | 由上层流程建模 | 事件/投影分离 | Effect ledger、补偿、审计与幂等 | 全量 Event Sourcing/CQRS 平台化 |

## 设计流派分析

### 1. Durable execution：Temporal、Step Functions、Durable Functions

#### 事实

- Temporal Workflow code 必须对重放保持确定性；API、LLM、数据库和其他非确定性交互应放入 Activities。Temporal 默认重试 Activity，并专门讨论 Activity 幂等；Event History 有容量限制，Continue-As-New 用新历史继续执行。参见 [Temporal Workflow Definition](https://docs.temporal.io/workflow-definition)、[Activity Definition](https://docs.temporal.io/activity-definition)、[Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)、[Workflow limits](https://docs.temporal.io/workflow-execution/limits) 与 [Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)。
- AWS Step Functions 的 callback task token 可以让流程暂停，等待外部系统或人工回传；单个 state/task/execution 的输入输出上限为 256 KiB，Standard execution history 上限为 25000 events。Standard 文档使用“exactly-once workflow execution”，Express 则提供至少一次或至多一次模型。参见 [Service integration patterns](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html)、[Quotas](https://docs.aws.amazon.com/step-functions/latest/dg/limits-overview.html) 与 [Workflow types](https://docs.aws.amazon.com/step-functions/latest/dg/choosing-workflow-type.html)。
- Azure Durable Functions 通过 event sourcing 重放 orchestrator，要求 orchestrator deterministic；外部事件用于人工审批、webhook 等异步输入，human interaction pattern 将外部事件与 durable timer 竞争。参见 [Orchestrator constraints](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-code-constraints)、[External events](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-external-events) 与 [Human interaction](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-phone-verification)。

#### 值得吸收

- 把确定性 Run 协调与非确定性 Domain Effect 分开；
- 每个 Effect 有稳定身份、显式超时、重试策略和最终记录；
- 工作流定义必须版本固定，长历史必须有归档或换代策略；
- 人工/外部响应必须作为持久事件，而不是内存回调；
- 输入、输出、历史和并发都必须有明确预算。

#### 不应复制

- 不应在 v2 阶段引入 Temporal 式代码重放约束、独立控制平面和 Worker fleet；
- 不应把 Step Functions 的“exactly-once workflow execution”解释成 BMC 外部命令 exactly-once；
- 不应把所有 openUBMC 操作拆成远程 Activity，以免增加部署、认证和排障复杂度。

#### openUBMC 含义

保留当前 Runtime Core，但内部形成两个清晰层次：`RunEngine` 只决定下一条确定性转换，`DomainExecutor` 承担可能失败、超时或重复的外部 Effect。当前 MutationJournal 应演进成明确的 Effect lifecycle，而不是继续由各 workflow 分支隐式约定。

### 2. Human process：Camunda/BPMN

#### 事实

- Camunda Job activation 有超时；超时后 Job 可重新分配，两个 Worker 可能同时处理同一个 Job，因此文档明确称其为至少一次交付，并要求 Worker 幂等。重试耗尽后产生 incident。参见 [Camunda job workers](https://docs.camunda.io/docs/components/concepts/job-workers/)。
- Camunda User Task 会让 process instance 停止并等待完成，且支持 assignment、scheduling、task updates、variable mappings 和 form。参见 [Camunda user tasks](https://docs.camunda.io/docs/components/modeler/bpmn/user-tasks/)。

#### 值得吸收

- Gate 是有身份、有状态、有负责人、有截止时间的工作项；
- “等待人”与“等待机器重试”是不同状态；
- 重试耗尽后应进入 incident/manual intervention，而不是无限自动推进；
- 人工提交需要审计原始输入、提交人、提交时间和所响应的 Gate 版本。

#### 不应复制

- 不应要求开发者用 BPMN 表达动态诊断、源码推理和 Agent 选择；
- 不应把每个技术步骤包装为 User Task 或 Service Task；
- 不应在当前规模引入建模器、表单引擎和流程治理套件。

#### openUBMC 含义

Gate 应从 Turn 内的临时 schema 提升为持久领域对象，但仍由 typed workflow definition 生成，而不是迁移到 BPMN。Operator UI/CLI 未来可以借鉴 User Task 的领取、提交、超时和审计语义。

### 3. DAG、Artifact 与 Asset：Argo、Airflow、Dagster、Prefect

#### 事实

- Argo 允许一个步骤产生 Artifact，并把该 Artifact 作为后续步骤输入；Artifact 存储在配置的 repository，并提供 GC 策略。参见 [Argo Artifacts](https://argo-workflows.readthedocs.io/en/latest/walk-through/artifacts/)。
- Airflow XCom 只为小数据设计，不应传递 dataframe 等大值；官方建议较大数据使用 Object Storage XCom backend。参见 [Airflow XComs](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/xcoms.html)。
- Dagster 把 Asset 定义为持久存储中的对象，并区分 Asset identity/dependency 与执行它的 op。参见 [Dagster Assets](https://docs.dagster.io/guides/build/assets/)。
- Prefect cache 由 cache key 与持久 result storage 联合工作；找到未过期结果时可以跳过重复任务。参见 [Prefect Caching](https://docs.prefect.io/v3/concepts/caching)。

#### 值得吸收

- 大 Evidence、日志、构建产物与验证输出通过 Artifact handle 传递；
- Artifact 使用内容摘要、类型、大小、创建者、保留策略和来源 Run 标识；
- 计算缓存必须绑定输入摘要、实现/工作流版本和目标身份；
- Artifact GC 与运行状态分离，不能依赖 Agent 上下文是否仍持有完整内容。

#### 不应复制

- 不应把所有诊断与修复表达成预定义静态 DAG；
- 不应让缓存命中替代实时 freshness、target epoch 或 Mutation reconcile；
- 不应把 Evidence、Artifact、Run state 混成一个 JSON document。

#### openUBMC 含义

当前 Observation 的 content-addressed source 是正确起点，但 `execute(start)` 不应要求 Agent 回传完整 Receipt。应改为回传 `observation_handle + digest`，Runtime 自行重建并验证。日志、构建包和原始证据也应统一走 Artifact/Evidence store，而 Turn 只携带引用与有界投影。

### 4. Agent orchestration：LangGraph、OpenAI Agents SDK、Google ADK、Microsoft Agent Framework

#### 事实

- LangGraph checkpointer 持久化 thread graph state，可用于恢复、fault tolerance 和 HITL；interrupt resume 时节点从头重新执行，因此 interrupt 前的副作用必须幂等。参见 [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。
- OpenAI Agents SDK 提供 agent loop、handoff、guardrails、sessions、resumable approvals 与 tracing；官方同时明确 server/application 拥有 deployment、tool implementations、state storage 与 approval decisions。参见 [OpenAI Agents](https://developers.openai.com/api/docs/guides/agents)。
- Google ADK 的 Sequential、Parallel 和 Loop workflow agents 可按预定义逻辑执行，而不调用模型决定编排；ArtifactService 将版本化二进制数据与 session state 分开。参见 [ADK Workflow Agents](https://google.github.io/adk-docs/agents/workflow-agents/) 与 [ADK Artifacts](https://google.github.io/adk-docs/artifacts/)。
- Microsoft Agent Framework 文档列出 HITL、checkpoint/resume、observability，以及 sequential、concurrent、handoff、group-chat 等编排模式。参见 [Agent Framework Workflows](https://learn.microsoft.com/en-us/agent-framework/workflows/)。

#### 值得吸收

- Agent 侧只感知“当前可决策状态”，不感知每个持久化细节；
- checkpoint/interrupt/approval 的交互模型适合 Turn 与 Gate；
- tracing 与 eval 应围绕语义 Run，而不是围绕 MCP 调用次数；
- deterministic workflow skeleton 与 model reasoning 可以并存：模型决定内容，Runtime 决定状态转换和副作用规则。

#### 不应复制

- Agent graph checkpoint 不能替代权威 Mutation ledger；
- handoff、node、thread 等框架术语不应渗入 openUBMC 领域 Interface；
- 不应让模型拥有重试、幂等键、fencing token 或回滚语义；
- 不应因 SDK 提供 approval/resume 就把持久化、鉴权和审计责任外包给 SDK。

#### openUBMC 含义

Agent framework 最适合作为上层消费者或 Adapter，而不是 Runtime Core。`Turn` 是 Agent-facing projection；Run、Gate、Mutation、Artifact 才是 Runtime 的权威对象。未来无论接入 Codex、OpenAI Agents SDK、LangGraph 或其他 Agent，均应复用同一语义 Interface。

### 5. 分布式一致性：Saga、Outbox、Event Sourcing、CQRS

#### 事实

- Saga 通过补偿事务撤销已完成步骤，但补偿逻辑本身增加复杂度，pivot 之后的 retryable transaction 需要幂等。参见 [Saga pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/saga)。
- Event Sourcing 提供追加事件和历史重建，但官方明确指出其复杂度、迁移成本、eventual consistency、事件版本与 upcaster 负担。参见 [Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)。
- CQRS 可以分离读写模型，但会引入额外复杂度、消息与最终一致性问题。参见 [CQRS pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs)。
- Transactional Outbox 解决数据库写入与事件发布的 dual-write 问题，但仍可能重复投递，因此 Consumer 必须幂等。参见 [Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)。

#### 值得吸收

- 使用 append-only Run/Effect ledger 提供审计、恢复和 reconcile 依据；
- 把 Command、Event、Mutation lifecycle 与当前投影区分开；
- 补偿只用于确实可逆的动作，并将补偿失败作为一等状态；
- 所有异步/重复处理都以稳定消息或 Effect identity 去重。

#### 不应复制

- 当前无需把 Runtime 全面改造成 Event Sourcing；
- 当前无需独立读写数据库、消息总线和投影集群；
- 不应把 rollback 当成所有副作用的通用逆操作；固件升级、硬件状态变化等可能不可逆或只能前向修复。

#### openUBMC 含义

应保留当前 SQLite WAL 与追加记录优势，但只把关键 Run、Gate、Effect 事实设计成稳定 ledger；普通查询投影和缓存仍可使用传统状态表。这样可以获得审计与恢复收益，而不承担完整 Event Sourcing/CQRS 的系统成本。

## 当前方向评估

### 方向正确的部分

1. **一个 Runtime Core**：Evidence、workflow、Mutation、reconcile、Replay 与治理资产不应分散回各 Skill。
2. **两个 Agent 语义入口**：`observe` 与 `execute` 是高杠杆 Interface，能隐藏 Runtime sequencing。
3. **Operator/CI 独立治理面**：原始 Evidence、Replay、Outcome promotion 和生命周期操作不应默认暴露给 Agent。
4. **MCP/CLI 作为薄 Adapter**：两者应投影同一 descriptor，而不是各自定义领域行为。
5. **有界 Observation**：scope、freshness、assurance 与 claim grounding 已经形成清晰语义。
6. **兼容面暂时保留**：在迁移遥测和 A/B 资格验证完成前保留 compatibility profile 是务实做法。

### 调研时尚未闭环的部分

| 风险 | 评估时表现 | 后果 | 优先级 |
| --- | --- | --- | --- |
| `execute` 输入无总预算 | 多个字符串/对象无上限，完整 Receipt 可回传 | 内存、Token、解析时间与攻击面不可控 | P0 |
| MCP stdio 无帧上限 | 先读完整行再解析 JSON | 超大输入可在 schema 校验前消耗资源 | P0 |
| ObservationReceipt 回传过重 | Agent 把完整投影重新提交给 Runtime | 重复上下文、篡改面和协议耦合 | P0 |
| Gate 缺少持久提交身份 | `response` 是自由 JSON，未绑定 Gate identity、version 与 submission digest | 重复提交、过期提交、跨 Gate 混用 | P0 |
| Mutation exactly-once 语义未明确 | 已有 idempotency 与 reconcile，但领域状态仍分散 | 故障切点下难以证明不会重复副作用 | P0/P1 |
| 内部 Port 太宽且 JSON 化 | 8 方法、`Mapping[str, object]` 透传 | 两工具退化为万能入口，类型与不变量分散 | P1 |
| RuntimeSDK 偏浅 | 每次调用传 Adapter，主要做包装 | seam 杠杆和 locality 不足 | P1 |
| 巨型文件与源码字符串测试 | 多个 1000–6500 行文件；文本断言 | 改动风险高、AI/人类导航困难 | P1 |
| Receipt 术语过载 | Observation、Domain、phase、idempotency 都叫 Receipt | 状态和责任难区分 | P1 |
| `execute` 资格验证不足 | A/B 主要覆盖 Observation | 无法证明三条交付路径恢复正确 | P0 |

### 总体判断

不要回退当前语义收缩，也不要继续扩张 Agent-facing MCP。下一阶段的主线应从“增加能力”切换为“建立执行语义”：typed Modules、Effect lifecycle、Gate identity、Artifact handles、版本与故障测试。只有当这些能力稳定后，新增 selector 或 workflow intent 才不会重新放大复杂度。

## 目标架构

```text
Agent / Agent SDK
        │
        │  observe(Query) / execute(Action)
        ▼
┌──────────────────────────────────────────────┐
│ Agent Semantic Gateway                      │
│ bounded validation · projection · redaction │
└──────────────┬───────────────────────────────┘
               │ typed commands/results
               ▼
┌────────────────────── Runtime Core ──────────────────────┐
│                                                          │
│  ObservationEngine        RunEngine                      │
│  selectors · assurance    deterministic transitions      │
│          │                      │                         │
│          ▼                      ▼                         │
│  Evidence/Artifact Store   Policy & Gate Module          │
│  handle · digest · GC      approval · submission         │
│                                 │                        │
│                                 ▼                        │
│                          DomainExecutor                  │
│                          idempotency · fencing           │
│                          timeout · reconcile             │
│                                 │                        │
│                                 ▼                        │
│                          Effect/Run Ledger               │
└──────────────────────────────────────────────────────────┘
               ▲
               │ governance queries/commands
      Operator / CI Interface

MCP Adapter and CLI Adapter stay outside the domain model and project the
same semantic Interface.
```

### 建议的 deep Modules

| Module | 建议 Interface | 隐藏的复杂性 |
| --- | --- | --- |
| `ObservationEngine` | `observe(Query) -> ObservationResult` | selector dispatch、capability discovery、freshness、assurance、claim grounding、source persistence |
| `RunEngine` | `start(StartCommand)`、`submit(GateSubmission)`、`advance(RunId)` | workflow version、deterministic cursor、Gate 创建、Outcome、step limit、恢复 |
| `DomainExecutor` | `execute(DomainCommand, EffectContext) -> DomainResult` | Adapter lookup、timeout、idempotency、target lease、fencing、journal、reconcile |
| `GateModule` | `open(GateSpec)`、`submit(GateSubmission)`、`expire(GateId)` | version、schema digest、submission identity、actor、deadline、审计 |
| `ArtifactStore` | `put(Content, Metadata) -> ArtifactHandle`、`get(Handle)` | content address、digest、size、retention、redaction、GC |
| `RunLedger` | `append(Fact)`、`load(RunId)` | 原子提交、sequence、schema version、crash recovery |

外部 `execute(Action)` 可以继续保持单一入口，但 Gateway 应立即将 Action 解码为以上 typed command。不要让 JSON 形状成为 Runtime 内部通用抽象。

## 建议领域模型

| 术语 | 定义 | 不是 |
| --- | --- | --- |
| `Case` | 一个长期存在的用户问题或治理聚合，可包含多个 Run | 单次 workflow attempt |
| `Run` | 绑定 workflow version、target identity/epoch 的一次执行链 | Agent 对话 thread |
| `Step` | Run 内由定义确定的计划转换 | 任意 MCP 调用 |
| `Command` | 请求执行某个确定操作或 Effect 的不可变意图 | 已经发生的事实 |
| `Event` | 已持久化、不可变的事实 | 可重复覆盖的 current state |
| `Mutation` / `EffectRecord` | 一个外部副作用从计划到确认的持久生命周期 | Domain 方法的临时返回值 |
| `Gate` | Run 暂停并等待特定外部决策的持久请求 | Turn 中匿名 JSON schema |
| `GateSubmission` | 对明确 `gate_id + version` 的不可变响应 | 可重复覆盖的 phase receipt |
| `Artifact` / `Evidence` | 内容寻址、可校验、独立保留的大载荷 | 直接内嵌在 Turn 的大对象 |
| `Turn` | 面向 Agent 的有界当前状态投影 | Runtime source of truth |
| `ObservationReceipt` | 面向 Agent 的有界 observation 投影/句柄 | Domain 执行结果或幂等记录 |
| `DomainResult` | DomainExecutor 返回的 typed 结果 | 观察 Receipt |
| `IdempotencyRecord` | 输入指纹、状态和已存结果的去重记录 | Effect lifecycle 本身 |

### Mutation 状态机

```text
planned → dispatched → applied
                  └→ failed
                  └→ unknown

unknown ── reconcile(same effect_id, target_epoch/fence) ──┬→ confirmed_applied
                                                           ├→ confirmed_not_applied
                                                           └→ manual_intervention
```

核心不变量：

- `unknown` 状态绝不能用新的 Effect identity 盲目重试；
- 所有重试沿用同一 `effect_id` 与输入摘要；
- target identity/epoch 或 fencing token 不匹配时必须 fail closed；
- 只有 fresh verification 或目标侧幂等查询能把 `unknown` 收敛为已应用/未应用；
- 无法证明时进入 `manual_intervention`，不能生成成功 Outcome。

### Gate 提交协议

建议最小字段：

```json
{
  "run_id": "run-...",
  "gate_id": "gate-...",
  "gate_version": 3,
  "submission_id": "submission-...",
  "response": {}
}
```

必须满足：

- Gate identity、version 与 schema digest 由 Runtime 生成并持久化；
- 同一 `submission_id + input digest` 重放返回原结果；
- 同一 submission identity 携带不同输入、错误 Gate 或旧 version 返回 conflict；
- 不同 submission 对已关闭 Gate 返回明确 conflict；
- actor 与 submitted time 由 Adapter/Runtime 派生，不由模型填写；
- Gate 打开、提交、过期、取消和人工接管都写入 ledger。

内部研发默认不要求 one-time secret Gate token，详见 ADR-0004。

### Artifact/Evidence 协议

Agent 与 Run state 只传以下有界元数据：

```json
{
  "handle": "artifact://sha256/...",
  "digest": "sha256:...",
  "media_type": "application/json",
  "size_bytes": 12345,
  "schema": "...",
  "created_by_run": "run-...",
  "retention_class": "run|audit|temporary"
}
```

读取时由 Runtime 校验 digest、访问范围和保留状态。ObservationReceipt、build output、log bundle 与验证证据都应复用该模型。

## 明确不采用的方向

1. **不恢复 19 个 Agent-facing Runtime 工具。** 新能力默认进入 selector、workflow intent 或 Operator Interface。
2. **不把 `observe/execute` JSON 作为内部万能抽象。** JSON 只存在于 Adapter/Gateway seam。
3. **不承诺外部副作用 exactly-once。** 对外使用可验证的至少一次 + 幂等/reconcile 语义。
4. **不在 v2/v2.1 引入 Temporal 集群。** 先借鉴语义，后以实际 HA/吞吐需求决定部署演进。
5. **不采用 BPMN 作为所有工作流的源语言。** 可以借鉴 User Task/incident，不引入完整流程套件。
6. **不把 openUBMC workflow 改造成通用 DAG 平台。** 静态 build/test 流程可以使用 DAG，Agent 技术工作流保留 typed state machine。
7. **不以 LangGraph/Agent SDK checkpoint 代替 Effect ledger。** Agent orchestration 和权威设备变更记录分层。
8. **不全面采用 Event Sourcing/CQRS。** 只为关键 Run/Effect/Gate 维护追加事实。
9. **不默认自动补偿所有 Mutation。** 只有可证明可逆的操作才定义 rollback/compensation。
10. **不在缺少遥测时增加 selector 或 workflow kind。** 新扩展必须由真实调用缺口和预算数据驱动。

## 建议 ADR 清单

| ADR | 决策 | 需要记录的关键理由 |
| --- | --- | --- |
| ADR-0001 | 一个 Runtime Core、两个默认 Agent operations、独立治理面 | 防止 Runtime 概念泄漏与工具面再次扩张 |
| ADR-0002 | MCP/CLI 是 Adapter，typed Modules 是内部 seam | 传输协议变化不应改变领域语义 |
| ADR-0003 | 确定性 RunEngine 与非确定性 DomainExecutor 分离 | 恢复、测试、重试和副作用规则的责任不同 |
| ADR-0004 | Effect 采用至少一次 + 幂等 + fencing + reconcile | 外部系统无法普遍提供 exactly-once |
| ADR-0005 | Gate 使用 versioned one-time submission identity | 防止重复、过期和错配响应 |
| ADR-0006 | 大 Evidence/Artifact 通过 handle + digest 传递 | 控制 Token、内存、历史增长和完整性 |
| ADR-0007 | Workflow definition/version 在 Run 创建时固定 | 保证重启与升级期间的解释一致性 |
| ADR-0008 | 只采用局部 append-only ledger，不全面 Event Sourcing | 获得恢复/审计收益，控制架构成本 |
| ADR-0009 | compatibility profile 的退役条件 | 遥测、调用方迁移、A/B 与回滚窗口均达标后移除 |
| ADR-0010 | 统一 Receipt/Result/Record/Submission 术语 | 消除当前多个 Receipt 的责任混淆 |

这些决策满足“难以逆转、未来读者会疑惑、存在真实权衡”三个 ADR 条件，适合在实现前落盘。

## 资格验证与测试矩阵

| 能力 | 场景 | 预期断言 | 当前状态 |
| --- | --- | --- | --- |
| Agent Interface | `tools/list` 默认 profile | 仅两个工具，schema 总量有界 | 已覆盖 |
| Observation | scope/freshness/assurance/预算 | 超范围 fail closed，Receipt ≤ 4 KiB | 已覆盖 |
| Observation 性能 | 固定模型、固定 target snapshot、10/20/30 对 A/B | Token、有效 Token、耗时满足阈值 | 10 对已通过 |
| Execute 输入 | 超长字符串、深层对象、宽对象、超大 Receipt | 在业务执行前拒绝，错误响应有界 | 256 KiB 总预算已覆盖；shape 细化待评估 |
| MCP transport | 超大单行、非法 UTF-8/JSON、并发取消 | 在解析前按帧预算拒绝，不影响后续请求 | 超大帧及后续合法请求已覆盖 |
| Observation handle | 正常、篡改 digest、GC 后引用、跨 target 使用 | 只接受可重建且 target 匹配的 handle | digest、target、restart 已覆盖；GC 待补 |
| Gate | 重复 submission、旧 version、错误 Gate、不同 input digest、并发响应 | 幂等重放或 conflict；ledger 完整 | 主路径已覆盖 |
| source-only | 正常、每个持久化切点重启、阶段失败/取消 | 不产生错误成功 Outcome | 部分覆盖，需全链路 |
| live-patch | dispatch 前后崩溃、目标响应丢失、重复 reconcile | 同一 effect_id 收敛，不重复替换 | 部分覆盖，需故障注入 |
| build-upgrade | build Gate/upgrade 前后重启、升级结果未知 | 同一 journal 恢复，fresh verification 后终结 | 部分覆盖，需真实/仿真 target |
| Mutation | timeout、连接断开、重复 delivery、target epoch 变化 | unknown 状态安全；fence 不匹配 fail closed | 缺失，P0/P1 |
| Artifact | digest 篡改、并发写入、保留/GC、跨 Run ACL | 完整性与访问规则可验证 | 部分覆盖 |
| Workflow version | 老 Run 遇到新代码、定义删除、schema 演进 | 固定旧版本或显式迁移，不能静默改义 | 缺失，P1 |
| Ledger | 每个 transaction cut point 进程 kill | 重启后无丢失、重复或非法状态 | 缺失，P1 |
| 性质测试 | 随机 action/event 序列 | 不变量始终成立，terminal 不复活 | 缺失，P1 |
| 兼容面退役 | usage telemetry 与调用方 inventory | 无活跃调用方且回退窗口结束 | 未建立 |

建议将“正常完成 + 任意持久化切点重启 + 外部 Effect 未知”定义为每条 Mutation workflow 的固定验收模板，而不是按项目临时补测试。

## 演进路线

### v2.0 发布前：封口与资格验证

目标：证明当前语义方向安全可发布，不再增加新能力。

1. 为 `execute` 定义统一 serialized input budget，并为每个字符串、数组、对象深度和属性数设置上限；错误输出也必须有界。
2. 为 MCP stdio 添加解析前 frame/line byte limit，并覆盖超大输入、连续合法请求和并发取消测试。
3. 将 `observation_receipt` 输入替换为 `observation_handle + digest`；Runtime 从 EvidenceStore 重建、校验 scope 和 target identity。
4. 引入 `gate_id + gate_version + schema digest + submission_id + input digest`，持久化
   Gate 与 Submission lifecycle；内部研发不增加 secret token。
5. 明确 Effect 状态机与 unknown reconcile 不变量，并把“exactly-once”从对外表述中移除。
6. 为 source-only、live-patch、build-upgrade 各完成正常、进程重启、故障注入三类全链路测试。
7. 把正式 A/B 摘要做脱敏后纳入仓库，记录模型、样本、阈值、无效 pair 与可复现实验方法。
8. 落盘核心 ADR，并定义 compatibility profile 的退役政策。

发布退出条件：所有 P0 测试通过；三条交付路径均能在未知副作用场景安全收敛；默认 Agent Interface 仍为两个工具；正式 A/B 不回归。

### v2.1：内部深化与可维护性

目标：让两个外部语义入口背后真正形成 deep Modules。

1. 提取 typed `ObservationEngine`、`RunEngine`、`DomainExecutor`、`GateModule`、`ArtifactStore`。
2. 将 Runtime Port 收缩到 2–3 个高杠杆操作，其他 seam 留在 Runtime 实现内部。
3. Domain Adapter 在 registry 初始化时注册一次，去除逐调用传 Adapter 的浅包装。
4. 按“观察、运行、持久化、传输、治理”这些变化原因拆分 `context_runtime.py` 与 `mcp.py`。
5. 用行为测试、contract tests 与 fake Adapters 替换源码字符串断言。
6. 完成 Receipt 术语拆分：`ObservationReceipt`、`DomainResult`、`EffectRecord`、`GateSubmission`、`IdempotencyRecord`。
7. 对较大的 `SKILL.md` 使用渐进披露：入口只保留触发条件、决策树和最短路径，详细规则下沉到 references。
8. 为 Run、Gate、Effect、Artifact 加 schema version 与兼容读取测试。

退出条件：外部 Interface 不变；核心流程可仅通过 typed Module Interface 测试；巨型文件不再承担多个独立变化原因；内部不再以任意 JSON 作为主协调模型。

### v2.x：按遥测扩展能力

目标：在不扩大 Agent Interface 的前提下增加覆盖面与运行质量。

1. 只根据真实 usage telemetry 增加 D-Bus property、verified active alarm、bounded log search 等 selectors。
2. 对独立 read-only selectors 并行执行，并复用 target connection/lease。
3. 建立 Artifact retention、GC、redaction、schema evolution 和审计策略。
4. 建立 workflow version pinning、迁移、弃用和 old-run support policy。
5. 增加 property-based tests、持久化 cut-point crash tests、网络 fault injection 和长运行 soak tests。
6. 建立语义级 observability：Run latency、Gate wait、unknown Mutation、reconcile outcome、Artifact bytes、Agent token，而不仅是 MCP call count。
7. 基于调用遥测和 A/B 结果逐步收缩 compatibility profile。

### v3：仅在规模证据出现后平台化

触发条件应至少包括：单进程吞吐持续成为瓶颈、需要跨主机 HA、多个团队需要独立 Domain Worker 部署，或单机 SQLite/文件 Artifact store 无法满足保留与恢复目标。

在触发后再评估：

- daemonized/HA Runtime；
- queue + worker execution；
- 稳定的 Domain plugin protocol；
- 外部 durable backend；
- 是否采用 Temporal 类 durable execution backend。

即使进入 v3，也应保留 `observe/execute` Agent Interface 与 Operator governance seam。基础设施可以替换，领域语义不能随平台重写。

## 待验证假设

1. `execute` 改为 handle-based input 后，完整工作流 Token 与 cached-input 会显著下降；需做与当前完整 Receipt 回传的配对 A/B。
2. typed Modules 与 Adapter 预注册会降低回归率和测试设置复杂度；需用改造前后变更影响面、测试数量与 defect 数据验证。
3. Observation selectors 并行化和连接复用能改善 wall time，且不会破坏同一 observation 的时间一致性；需固定 target snapshot 基准测试。
4. 单机 Runtime Core 足以支撑 v2.x 的并发与保留需求；需用目标并发、Artifact 规模和 crash-recovery SLO 做容量测试。
5. compatibility profile 的真实使用量足够低，可以在 v2.x 退役；需先建立匿名 usage telemetry 或明确调用方清单。

## 最终判断

openUBMC Agent Workflow 不需要换方向，而需要把当前方向做完整：

- 对 Agent：保持两个语义入口；
- 对 Runtime：从 JSON 路由器演进为 typed durable execution core；
- 对副作用：采用可验证的至少一次、幂等、fencing 与 reconcile；
- 对人机协作：把 Gate/Submission 变成持久对象；
- 对数据：让 Evidence/Artifact 通过句柄流转；
- 对平台化：等待规模证据，不提前选定 Temporal、BPMN、DAG 或 Event Sourcing。

这条路线既保留了当前 Runtime 已经获得的证据完整性、审计和恢复价值，也避免重新把其内部复杂度暴露给 Agent。

## 一手资料

以下页面均于 2026-08-19 访问：

### Durable workflow

- [Temporal: Workflow Definition](https://docs.temporal.io/workflow-definition)
- [Temporal: Workflow Execution](https://docs.temporal.io/workflow-execution)
- [Temporal: Activity Definition](https://docs.temporal.io/activity-definition)
- [Temporal: Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)
- [Temporal: Workflow Execution Limits](https://docs.temporal.io/workflow-execution/limits)
- [Temporal: Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)
- [AWS Step Functions: Service Integration Patterns](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html)
- [AWS Step Functions: Quotas](https://docs.aws.amazon.com/step-functions/latest/dg/limits-overview.html)
- [AWS Step Functions: Workflow Types](https://docs.aws.amazon.com/step-functions/latest/dg/choosing-workflow-type.html)
- [Azure Durable Functions: Orchestrator Code Constraints](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-code-constraints)
- [Azure Durable Functions: External Events](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-external-events)
- [Azure Durable Functions: Human Interaction Pattern](https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-phone-verification)

### Human process and DAG/data workflow

- [Camunda 8: Job Workers](https://docs.camunda.io/docs/components/concepts/job-workers/)
- [Camunda 8: User Tasks](https://docs.camunda.io/docs/components/modeler/bpmn/user-tasks/)
- [Argo Workflows: Artifacts](https://argo-workflows.readthedocs.io/en/latest/walk-through/artifacts/)
- [Apache Airflow: XComs](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/xcoms.html)
- [Dagster: Assets](https://docs.dagster.io/guides/build/assets/)
- [Prefect: Caching](https://docs.prefect.io/v3/concepts/caching)

### Agent orchestration

- [LangGraph: Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph: Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [OpenAI: Agents](https://developers.openai.com/api/docs/guides/agents)
- [Google ADK: Workflow Agents](https://google.github.io/adk-docs/agents/workflow-agents/)
- [Google ADK: Artifacts](https://google.github.io/adk-docs/artifacts/)
- [Microsoft Agent Framework: Workflows](https://learn.microsoft.com/en-us/agent-framework/workflows/)

### Distributed consistency patterns

- [Azure Architecture Center: Saga](https://learn.microsoft.com/en-us/azure/architecture/patterns/saga)
- [Azure Architecture Center: Event Sourcing](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
- [Azure Architecture Center: CQRS](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs)
- [AWS Prescriptive Guidance: Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
