# openUBMC Agent Workflow 后续演进档案

日期：2026-08-26
当前实现基线：GitHub `main` 已发布 v2.0.1 并继续进入发布后维护演进
P2 生命周期完成基线：`2564f3572fd82668dfd90ba3bd2e3439d021ec63`
历史 v2 发布资格 source：`e7dc74c`
历史 lock-only commit：`7dc350c`（`superseded-unpublished`，不得直接发布）
已发布 v2.0.0 source：`f27db4f`
已发布 v2.0.0 lock-only commit：`c0e095a`
P2 生命周期持续资格 source：`5d9b7e32de563ab85c3c31e7b75d122cd3db4545`
用途：后续讨论入口、决策索引和实施路线；详细论证仍以链接文档为准。
机器可读完成证据：[roadmap-completion.json](roadmap-completion.json)。

## 1. 当前总体判断

openUBMC Agent Workflow 不需要再次换方向。正确路线是：

```text
保留 Runtime Core
    │
    ├─ 对 Agent：继续收敛为 observe / execute
    ├─ 对内部：从 JSON 路由深化为 typed RunEngine
    ├─ 对副作用：保留 MutationJournal + reconcile
    ├─ 对治理：维持独立 Operator / CI Plane
    └─ 对分布式：等待明确规模和部署证据
```

M0 至 M6 的既定范围已完成：Incident 闭环、compatibility 退役、恢复与压力资格、
ArtifactRef/Log Bundle、Domain Pack/只读能力、Evidence 检索和 Skill 渐进披露均已进入
canonical `main`。完整 execute A/B、Release Gate 与 main CI 通过。后续不扩张核心架构，
只按真实遥测继续长期资格和条件式能力演进。

### 1.1 当前实现检查点

| 里程碑 | 当前状态 | 剩余工作 |
| --- | --- | --- |
| M0 资源边界与决策更新 | 完成 | 保持资源边界回归测试 |
| M1 typed seam 与 source-only | 完成 | 保持 retired input rejection 与 old-event upcaster 回归测试 |
| M2 Live Patch 可靠性 | 完成 | 继续扩充真实 target fault evidence |
| M3 Build-Upgrade Artifact flow | 完成 | 继续扩充长时间 soak 与容量证据 |
| M4 权威收敛 | 完成 | 保持 compatibility writer/profile 已退役和 Incident lifecycle 指标稳定 |
| M5 Domain Pack | 完成 | 内建与扩展 Pack 共用 typed 作者契约和 Pack-set conformance suite；Log Bundle index/query/export 为真实本地 READ_ONLY Pack |
| M6 证据驱动扩展 | 完成（既定范围） | 后续扩展继续由 Incident、容量和真实调用缺口决定 |
| P2 生命周期持续资格 | 完成 | 旧 Run fixture、Artifact retention/GC 容量和软投影策略已进入 canonical `main` 与持续 CI |

最终兼容退役 source 的完整 execute A/B 为 10 组有效、0 无效，`decision=passed`；
Release Gate 为 13/13 passed、`promotable=true`，证据 digest 为
`sha256:e18fdbfcbc04e84a5ba79f2160728cedc11d4a873a40e7091f2beaa35c9b2a67`；
P2 PR CI run `32933867020` 与 main CI run `32934292608` 的 CI contract 和完整仓库验证均
通过。`v2.0.0` 已于 2026-08-27 从 `f27db4f -> c0e095a` 的 source-plus-lock 拓扑正式发布。
此前 `e7dc74c -> 7dc350c` 仍只作为历史资格证据保留，状态为
`superseded-unpublished`。下一维护版本必须高于已发布的 `v2.0.1`；当前计划版本为
`v2.0.2`，但只有 fresh Runtime 产品闭环完成后才能选择新的最终 source、重新资格化并生成
新的 lock-only commit。

### 1.2 产品北极星

目标不是建设一个通用 Agent 编排平台，而是形成 **openUBMC 的可信执行底座**：即使模型重复调用、进程中断、目标响应丢失或操作结果未知，系统仍能回答“观察依据是什么、当前 Run 在哪里、危险 Effect 是否执行过、下一步由谁决策、什么证据允许宣告成功”。

三层职责必须长期稳定：

| 层次 | 负责 | 不负责 |
| --- | --- | --- |
| Skill / Model | 理解用户意图、选择领域路径、分析有界事实、填写 Gate | 持久状态、Effect identity、重试安全、目标 fencing、终态成功 |
| Runtime Core | Evidence、Run/Gate/Incident、Effect 安全、恢复、验证和 Outcome | 开放式通用推理、替代全部 Skill 知识 |
| Operator / CI Plane | 审计、Replay、Incident 处置、发布与生命周期治理 | 成为普通 Agent 的默认工具面 |

后续所有功能提案先判断它属于哪一层。需要 Agent 理解 Runtime 内部 sequencing 才能使用的能力，通常说明 Module 仍然过浅；需要 Runtime 相信模型自报状态才能保证安全的能力，必须否决。

### 1.3 方向评价指标

不能再只用“工具数”和单次调用延迟判断成败。后续采用四组指标：

| 维度 | 核心指标 |
| --- | --- |
| 安全 | false-success 数、重复危险 Effect 数、unknown 使用新 identity 数、过期 Evidence 接受数，目标均为零 |
| Agent 成本 | 每个 actionable Turn 的模型往返、非缓存 Token、重复 Artifact bytes、time-to-next-actionable-Turn |
| 恢复 | crash-cut 覆盖率、reattach 成功率、unknown 收敛时间、需要人工介入的 Incident 比例 |
| 可演进性 | Gateway Interface 面积、跨 Module 修改数、旧 profile 调用占比、行为测试对内部重构的稳定性 |

功能吞吐和 selector 数量只在这些指标不退化时才有意义。

## 2. 已保存的信息与事实源

### 2.1 仓库内项目文档

| 文档 | 作用 | 状态 |
| --- | --- | --- |
| [市场工作流方法调研](workflow-design-market-research.md) | Temporal、Durable Functions、Step Functions、Camunda、DAG、Agent Framework 与分布式模式的统一比较 | 完成 |
| [架构裁决](workflow-architecture-arbitration.md) | WorkflowDefinitions/RunEngine、Turn/Ack、Outbox/Inbox、Incident、模型边界和故障切点的最终取舍 | 完成 |
| [Agent Semantic Gateway](agent-semantic-gateway.md) | 当前 `observe/execute` Interface、profile、预算与恢复能力 | 已实现基线 |
| [Runtime Stability Qualification](runtime-stability-qualification.md) | duplicate storm、SQLite 并发、crash-cut、容量和 restart soak 的可复核资格 | 已实现 CI 基线 |
| [领域上下文](../CONTEXT.md) | 产品边界、统一术语、事实所有权和跨版本不变量 | 完成 |
| [架构决策记录](adr/README.md) | 五项难以逆转的已接受决策及其触发条件 | 完成 |
| [路线图完成审计](roadmap-completion-audit.md) | 逐批绑定 Issue、PR、测试、资格证据与 GitHub CI | 完成 |
| [外部深度研究对照](external-workflow-research-reconciliation.md) | 对 ChatGPT Share 深度报告逐项裁决，区分直接采纳、改造采纳和延后项 | 完成 |
| 本文档 | 连接事实、决策、阶段路线和后续讨论 | 持续更新 |

### 2.2 实验工件

| 工件 | SHA-256 | 说明 |
| --- | --- | --- |
| `/home/workspace/openubmc-ab-isolated-20260818/results-20260818-231859/all_metrics.json` | `3237082d2afcbecbd79fd578b7ea5e4099330680f725bec7c53bfb8ce94677bc` | 早期隔离实验原始指标；证明旧 MCP Interface 存在严重 Token/耗时回归 |
| `/home/workspace/openubmc-ab-complex-20260818/summary.md` | `e65cea0f02736a6daa7e7322afccad369e96e095a99cfdb1c640321a81f98e46` | 复杂诊断对比；新版 Evidence 完整性更高，但有效 Token 与耗时仍回归 |

正式 Semantic Gateway 10 对 A/B 的已确认比率：

- total tokens：`0.234815`；
- non-cached input + output：`0.320367`；
- wall time：`0.371208`。

后续完整 execute 资格同样通过，证明两入口 Interface、重启恢复和 Mutation recovery
已达到 v2 候选基线；后续 A/B 用于防回归和 M6 扩展判断。

### 2.3 会话与机器可读归档

- Codex session：`01a00e35-8cb0-7fe1-940a-d888dc83fe94`；
- Obsidian note：`/mnt/d/Obsidian/vaults/obsidian/Codex Sessions/2026/08/我们现在来审视我们的openubmc-agent-workflo.md`；
- machine-readable artifact：`/mnt/d/Obsidian/vaults/obsidian/Codex Sessions/.codex-session-memory/artifacts/01a00e35-8cb0-7fe1-940a-d888dc83fe94.json`；
- session index：`/mnt/d/Obsidian/vaults/obsidian/Codex Sessions/Session Index.md`。

原始会话和工具活动保存在 JSON artifact 中；仓库文档只保留可复核结论，不复制巨量原始输出。

### 2.4 外部深度研究报告

- 来源：<https://chatgpt.com/share/6a85aeb3-472c-83ea-b5b1-0c3d2fd42478>；
- 结论：产品边界、安全原则、Artifact、Gate、模型职责和测试方法与当前路线高度一致；
- 分歧：报告过早引入 Agent 可见 CommandAck/polling、v2.1 Outbox/Inbox、强制多 Run Case 聚合，并把状态权威放到与当前职责不符的 Workflow Kernel；
- 裁决：保持当前 `RunEngine` 唯一权威和 Turn 语义，Outbox/Inbox 等待真实进程 seam；吸收 Effect class、必要版本 pin、执行总预算、受限 PlanProposal 与扩展 Benchmark；
- 详细对照见[外部深度研究报告与当前架构裁决对照](external-workflow-research-reconciliation.md)。

## 3. 已确认事实

### 3.1 产品与 Interface

- 默认 Agent profile 已只暴露 `observe` 和 `execute`；
- Agent 与 Operator profile 保持分离；已退役的 compatibility profile 被明确拒绝；
- Observation scope、Receipt、Turn 和 Gate schema 已有输出预算；
- Debug Observation 只执行 selector 声明的 preflight surface 及其必要传输依赖，
  cache 与 assurance refresh 不扩大采集范围；
- `execute` 支持 `start | respond | resume | control`；
- Observation A/B 已证明两个语义入口的方向正确；
- 当前真正风险不在工具数量，而在长期运行数据、真实 target fault evidence 和按遥测选择
  后续能力。

### 3.2 内部状态权威

- `WorkflowDefinitions` 只负责 definition、step identity 和 semantic cursor；旧
  `WorkflowKernel` 名称仅作为 import alias 保留；
- `SemanticRuntimePort` 只暴露 typed `observe` 和 `execute`；
- Gateway 只负责有界解码和投影，不再组合 `phase_record → workflow.next`、reconcile 或
  terminal Session Outcome 写入；
- `RunEngine` 统一处理 Gate、自动推进、unknown Mutation reconcile、Incident 和 terminal
  Outcome 投影；
- `DomainExecutor` 在 Runtime 构造时注册 Adapter，只读传输失败有限重试，Mutation 不盲目
  重放；
- `ContextRuntime` 继续承载 repository 与 Evidence 实现；原生 Agent Gate response 由
  RunEngine 提交持久 Gate；兼容 writer/profile 已由零增长 telemetry 与同源 Release Gate
  完成退役，历史事件继续由显式 upcaster 转换；
- Session Outcome 只从持久 `RunOutcomeRecorded` 投影，terminal replay 不重复写 Run Outcome、
  Closeout 或治理记录。

### 3.3 Mutation 与恢复

- `MutationJournal` 已提供稳定 operation identity 与 fingerprint；
- unfinished journal 会阻断同一 target 的新 Mutation；
- `effects_started` 是 fail-closed 安全边界；
- 明确远端拒绝可以回到 `replan_required`；
- effect 可能开始后的异常必须视为 outcome unknown；
- recovery 必须先做只读 inspection；
- unknown 只能使用相同 identity 收敛到 replan、verify、rollback 或 manual；
- fresh verification 必须发生在新的 target epoch；
- rollback 需要显式授权。

### 3.4 规模与部署

- 当前是单进程、同进程 Domain 调用；
- 当前没有 Broker、跨进程 Worker 或 active-active Runtime；
- 当前没有数据库提交与消息发布的 dual-write；
- 因此正式 Outbox/Inbox、共享 fencing 和 Worker fleet 尚无采用依据。

## 4. 已裁决的方向

| 主题 | 决策 | 状态 |
| --- | --- | --- |
| Runtime Core | 保留一个状态与安全语义权威 | 已确认 |
| Agent Interface | 继续只保留 `observe/execute` | 已确认 |
| MCP/CLI | 作为 transport Adapter，不承载领域模型 | 已确认 |
| 内部 Interface | typed Query/Command/Result，不继续透传万能 JSON | 已确认 |
| Workflow 模块 | `WorkflowKernel` 校正为 `WorkflowDefinitions` | 已确认 |
| Run 状态 | `RunEngine` 成为唯一状态转换权威 | 已确认 |
| execute 结果 | 返回下一 Gate、Incident、running reattach point 或 Outcome 的 `Turn` | 已确认 |
| CommandAck/polling | 不对普通 Agent 暴露；将来仅存在于 transport/worker Adapter 后 | 已确认 |
| Gate | 使用 ID、版本、schema digest、submission identity 和输入 digest；内部研发不使用 secret token | 已实现 |
| Incident | 自动 reconcile 仍无法收敛时形成明确 Turn；已固化恢复路径、允许命令、去重和持久指标 | 已实现基线 |
| Artifact | 使用 handle + digest + metadata，Run state 不内联大对象 | 已实现基线 |
| Event model | 局部 event-backed ledger，不全面 Event Sourcing/CQRS | 已确认 |
| Model invocation | 当前留在 Runtime 外；未来作为非确定性 Effect | 已确认 |
| Outbox/Inbox | 当前不引入，跨进程或提前 Ack 时才强制采用 | 延后 |
| Temporal/BPMN/DAG | 借鉴语义，不直接作为 v2 内核 | 延后/否决 |
| Dynamic Action Catalog | 当前 Action 数量很少，先解决输入边界 | 延后 |

## 5. 目标 Module 形态

```text
Agent / Agent SDK
        │
MCP / CLI Adapter
        │  bounded decode / redaction / projection
        ▼
Agent Gateway
        │  ObservationQuery / RunCommand
        ▼
┌──────────────── Runtime Core ────────────────┐
│                                              │
│ ObservationEngine       RunEngine            │
│ selectors / auto policy sole state authority │
│        │                    │                 │
│ ArtifactStore          WorkflowDefinitions   │
│                       Gate / Incident         │
│                       RunStore / ledger       │
│                       DomainExecutor          │
│                              │               │
│                       MutationJournal         │
│                       target lease / epoch    │
└──────────────────────────────────────────────┘
        ▲
        │ governance Query/Command
Operator / CI Plane
```

推荐的 Gateway-to-Runtime Interface 只有两个高杠杆入口：

```python
class SemanticRuntimePort(Protocol):
    def observe(self, query: ObservationQuery, *, task_id: str, operation_id: str) -> ObservationResult: ...
    def execute(self, command: RunCommand, *, task_id: str, operation_id: str) -> RunTurn: ...
```

`RunEngine` 自身进一步收敛为：

```python
class RunEngine:
    def execute(self, command: RunCommand, *, task_id: str, operation_id: str) -> RunTurn: ...
```

Interface 的目标不是减少方法数字本身，而是让调用方无需理解 definition pinning、Gate lifecycle、事件提交、Effect 调度、reconcile 和 Outcome 形成过程。

## 6. 实施主线与持续方向

### 主线 A：发布安全与资格验证

这是 v2 收口时的第一优先级，既定范围已经完成。新 selector 或 workflow family 仍需以
真实缺口和不退化证据为前提。

1. `execute` 整体 serialized input budget；
2. 字符串、数组、对象深度和属性数上限；
3. MCP stdio 解析前 frame/line byte limit；
4. ObservationRef/ArtifactRef 替代完整 Receipt 回传；
5. 持久 Gate identity 与一次性提交；
6. Live Patch、Build-Upgrade 全故障切点测试；
7. unknown Mutation 绝不产生 success Outcome；
8. 提交正式 A/B 摘要、环境指纹和 artifact digest；
9. 落盘关键 ADR。

### 主线 B：Runtime 内部深化

1. 先引入 typed Query/Command/Result 和两方法 Port；
2. 迁移期间用 compatibility Adapter 接住旧 operations，完成后 move-and-delete；
3. Gateway 切换后，将状态协调移入 `RunEngine`；
4. `WorkflowKernel` 改为 `WorkflowDefinitions`；
5. Domain Adapter 构造时注册，形成 `DomainExecutor`；
6. Gate、Submission、Incident 和 Outcome 统一由 RunEngine 提交；
7. Session Outcome 改为 terminal Run 的治理投影；
8. 用行为测试逐步替换源码字符串测试；
9. 完成旧 event/schema 的 upcaster 与兼容读取；
10. 为 Domain Action 建立 Effect class 与默认 retry/reconcile 行为；
11. 固定恢复和审计真正依赖的 Action、Workflow、DomainExecutor 与 Projector version。

迁移原则是 move-and-delete，不在旧逻辑外永久叠一层新状态机。

### 主线 C：Agent 成本与开发体验

1. 保持每个语义 Gate 最多一次模型可见往返；
2. 不向 Agent 暴露 Ack、poll、revision、attempt、phase_record 或 Evidence offset；
3. 小结果 inline，大结果传 ArtifactRef；
4. 相同 Evidence/Artifact 不重复进入上下文；
5. 对较大 `SKILL.md` 使用渐进披露；
6. Developer、Build 等重阶段可以隔离工作上下文，只返回小型结构化结果；
7. Runtime 内并行只读 selector、复用连接和稳定能力事实；
8. 以 time-to-next-actionable-Turn 衡量速度，而不是 time-to-Ack。

### 主线 D：条件式平台化

v3 不是预定实现，而是满足触发条件后的选项：

```text
完成前 durable Ack      -> Outbox
WorkOrder 跨进程        -> Outbox + Worker Inbox
Worker result 跨进程    -> Inbox + result dedupe
跨主机 Mutation Worker  -> shared store + monotonic fencing
第二个 active Runtime   -> external durable backend
```

进入 v3 后仍保持 `observe/execute` 和现有领域语义。Temporal 或其他 durable engine 只能作为底层 Adapter 候选，不能倒逼重写 Agent Interface。

## 7. 分阶段路线

### P0：v2 发布收口（已完成资格）

| 目标 | 验收 |
| --- | --- |
| 输入与传输有界 | 超大输入在业务执行前拒绝；错误响应仍有界 |
| Observation handle 化 | Runtime 可从 handle/digest 重建；篡改、跨 target、GC 后 fail closed |
| Gate 持久身份 | 重复提交幂等；并发单赢家；旧版本、错误 Gate 和不同输入 conflict |
| Mutation 恢复证明 | Live Patch/Upgrade 每个切点不重复危险 Effect |
| 发布证据 | 最终 source 的 451 项 Runtime 测试、10 组 execute A/B、13 项 Release Gate 与 main CI 已通过 |
| ADR | 产品 Interface、状态权威、Effect、Gate、Artifact 和分布式触发条件落盘 |

### P1：v2.x 运行闭环与内部收敛（既定范围已完成）

| 目标 | 验收 |
| --- | --- |
| Incident 闭环 | 内建 Incident 已有确定的 retry、reconcile、correction-then-resume、cancel 或 terminal 路径；Operator 指标从持久 Run ledger 派生并支持重启恢复 |
| Compatibility 收敛 | 已完成：feature-level 持久遥测与同源完整资格通过，旧 writer/profile 已删除，old-event reader 保留 |
| Module locality | compatibility、EvidenceStore、Runtime composition 从 MCP transport 中集中 |
| 测试稳定 | duplicate storm、SQLite 并发、crash-cut、capacity 与 restart soak 已纳入 release qualification；property 与 network fault injection 继续深化 |

### P2：v2.x 按遥测扩展

1. workflow version migration 与 old-run support：已冻结首个支持 fixture，当前 writer 版本、read-only legacy kinds 和未知版本拒绝策略进入机器可读持续资格；新 definition 版本出现前不引入迁移 writer；
2. Artifact retention、redaction 和 GC：持久 Repository、内容寻址、作用域校验、显式释放、红化派生与 64 引用容量/重启/共享内容 GC 资格已完成；继续积累生产保留时长遥测；
3. selector 并行与连接复用；
4. D-Bus、active alarm、bounded log search 等按真实调用缺口增加；
5. compatibility 历史遥测审计与 old-event upcaster 保留窗口：支持窗口已机器可读并由 fixture 回放；reader 保持只读，未知 schema/version 明确拒绝；
6. 扩展长时 soak、property-based 和 network fault injection：Artifact 生命周期容量场景已进入持续资格；property-based、真实网络 fault 和更长 wall-clock soak 仍按失败数据扩展；
7. `ModelInvocationRecord` 与受限 `PlanProposal -> PlanRevision` 原型已完成；确定性评估证明
   持久重放、严格 JSON、Gate/compensation 语义和边界安全；六组配对任务中两条路径均为
   6/6 有效且各含 4 个语义 Gate turn，候选额外产生 6 次模型调用，未证明 Agent-turn 或
   plan-validity 杠杆，因此保持 isolated，不接入生产 `RunEngine`。详见
   [模型规划原型](model-planning-prototype.md)。

### P3：v3 分布式执行

推荐迁移顺序：

1. 本地 async Ack + SQLite Outbox；
2. 先分离 read-only Worker；
3. 增加 Inbox 和结果去重；
4. 共享 Run/Artifact store 与 distributed fencing；
5. 最后迁移 Mutation Worker；
6. 根据运维成本再评估 Temporal 类 backend。

Mutation Worker 必须最后迁移，因为它需要最严格的 effect-start handshake、target fencing 和 unknown recovery。

## 8. 下一轮最值得讨论的设计点

### 8.1 `RunEngine.execute` 的原子提交模型

已落地答案：一次 command 形成一次持久 Decision，包含 Run events、Gate/Incident 变化和
Effect intent；Domain Effect 的真实执行结果通过同一 Effect identity 回到 RunEngine。
旧 Case/Run events 使用显式 upcaster，不按新定义静默改写。

### 8.2 Gate 的最小安全协议

已落地答案：`gate_id + gate_version + schema digest + submission identity + input digest`。
内部研发流程不使用 one-time secret token；Agent 不负责生成 actor 或时间戳，缺省
submission identity 由 Adapter 从持久 Run/Gate binding 派生。详见 ADR-0004。

### 8.3 Case 与 Run 的长期关系

推荐默认答案：本轮迁移不拆新聚合，继续把当前 `case_id` 作为 Run 的存储兼容身份；只有出现同一用户问题需要多个独立 Run、比较或回滚链时，再引入 Case grouping。

### 8.4 长任务的 reattach

推荐默认答案：普通 Agent 不 poll。Runtime/Adapter 尽量等待下一 actionable Turn；超出 caller deadline 时返回有界 `running` Turn，重试同一 command 或 `ResumeRun` 重新附着。

### 8.5 event-backed 的边界

推荐默认答案：只对 Run、Gate、Incident、Effect reference 和 Outcome 保留追加事实；Observation Blob、日志和 Artifact bytes 不进入 event history；正常读取使用 projection，不要求每次 replay 全历史。

### 8.6 v2 与 v2.1 的发布边界

`v2.0.0` 已从 source `f27db4fc9694797c050cd9bdb26ef1a791e31beb` 和 lock-only commit
`c0e095af0cbed285cd84ff3eecf05b578644999d` 正式发布。更早的
`e7dc74c052f3874d3d9214ce0cfae8949a397765 -> 7dc350cd3ecf2ffab2d1d4db89d4bac81f1ccec4`
仍是 `superseded-unpublished` 历史候选，不得用于发布。`main` 是持续前进的开发分支；它保留
的 `release-lock.json` 是历史发布快照，不能证明当前 mutable `main` 的树身份。`v2.0.1`
已正式发布。后续维护版本当前计划为 `v2.0.2`，必须在 fresh Runtime 产品闭环成功后选择
新的最终 source，重跑完整 execute A/B、Runtime qualification、Release Gate 与 GitHub CI，
再生成只修改 `release-lock.json` 的新 lock-only commit；在此之前不得宣称新的产品闭环或
发布候选已经完成。

### 8.7 产品闭环与维护候选资格

仓库级维护候选资格与产品级 fresh closeout 分开判定。持续资格会检查产品证据契约、正式
客户端矩阵、DSH 评测隔离、MCP 任务归属与退出清理，以及重复终态诊断投影；这些检查不需要
真实 BMC 或凭据。630 NVMe 历史材料已机器验真为 `historical-product-validated`，但因为发生
在当前 Runtime 之前，不能追认为新的 Run/Outcome。

fresh 产品晋级仍必须重新取得：当前 Runtime Run ID、终态 Outcome、独立升级授权、目标与
回滚包、官方 UT/编译、HPM 身份和协议精确的实机验收。详见
[产品闭环资格](product-closeout-qualification.md)与
[持续收口资格](continuous-closeout-qualification.md)。

## 9. 需要持续验证的假设

1. ObservationRef 会显著降低完整 execute 的 cached-input；
2. Turn-to-next-Gate 在 30–120 秒 Effect 下仍能避免额外模型调用；
3. typed Port 与 Adapter 预注册会降低变更影响面和测试设置成本；
4. 单进程 Runtime 足以满足 v2.x 的并发、存储和恢复 SLO；
5. Case 暂不独立建模不会阻碍多阶段 Run；
6. Incident 正式化能改善 unknown recovery，而不会形成新的 Operator 工具膨胀；
7. Skill 渐进披露与阶段上下文隔离能显著降低完整 Build-Upgrade Token；
8. compatibility profile 退役后，历史 Run 仍可通过显式 upcaster 稳定读取：首个冻结 fixture 与当前 reader 已通过持续资格，继续保留长期回归。
9. Runtime 内模型规划只有在真实任务 A/B 证明降低 Agent turns 或减少 plan defect 后才值得
   接入；当前确定性评估结论为 `isolate`。

所有假设都必须通过原型、fault injection、A/B 或使用遥测验证，不能直接升级为架构事实。

## 10. 停止条件与护栏

- 任一 Scope violation、unsupported claim、过期 Evidence 接受或 false success：阻断发布；
- 任一危险 Mutation 在恢复中发生重复 Effect：阻断发布；
- unknown 使用新 operation identity 重试：阻断发布；
- Observation 窄查询在新 Interface 下仍无法稳定达到 `1.15×` 硬上限：保留轻量 CLI/Adapter 为默认查询路径；
- 完整 Workflow 同质量正常路径仍明显超过旧基线：Runtime 默认用于 Mutation、恢复和审计，不强行成为所有任务控制面；
- 无跨进程、HA、权限隔离或容量证据：禁止提前引入 Broker、Outbox/Inbox 和 Worker fleet；
- 新 selector、Action family 或顶层工具必须有真实使用缺口、预算和测试证据。

## 11. 更新约定

后续每次讨论形成稳定结论时：

1. 更新本文的决策表和阶段路线；
2. 难以逆转的选择写入独立 ADR；
3. 实验结果记录 commit、环境、样本、阈值和 artifact digest；
4. 当前任务状态写入 Codex active memory；
5. 完整会话同步到 Obsidian；
6. 只把可跨会话复用的规则写入 project lesson store。

本文是讨论索引，不替代实现测试、ADR、Release Lock 或原始实验工件。
