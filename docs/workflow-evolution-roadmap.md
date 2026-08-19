# openUBMC Agent Workflow 后续演进档案

日期：2026-08-19
实现分支：`refactor/run-engine-core`
实现基线：GitHub `main` 的 `4e6172f`
用途：后续讨论入口、决策索引和实施路线；详细论证仍以链接文档为准。

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

现阶段已经证明“外部语义收缩”有效，下一阶段应证明“内部状态权威、危险副作用恢复和完整 execute 路径”可靠。功能扩张应暂时让位于架构收敛和资格验证。

### 1.1 当前实现检查点

| 里程碑 | 当前状态 | 剩余工作 |
| --- | --- | --- |
| M0 资源边界与决策更新 | 256 KiB Agent request/stdio frame、有界 `readline`、后续请求恢复、ADR-0004 已完成 | 在正式发布配置上复核阈值 |
| M1 typed seam 与 source-only | `SemanticRuntimePort.observe/execute`、RunEngine、持久 Gate、重启与提交幂等的纵向切片已完成 | 逐步删除仅服务旧调用方的输入形状 |
| M2 Live Patch 可靠性 | DomainExecutor、Mutation 单次执行、自动 reconcile、Incident、deferred fresh verification 主路径已完成 | 补齐真实/仿真 target 的全部 effect-start cut points |
| M3 Build-Upgrade Artifact flow | 强 target/run 绑定 ArtifactRef、build Gate、running reattach、upgrade reconcile 与重启主路径已完成 | 补 upload accepted 丢响应、activation/reboot 长任务矩阵 |
| M4 权威收敛 | Agent 主路径 sequencing、Run Outcome 与 Session Outcome 投影已移入 RunEngine | 删除 `phase_record` persistence bridge，固化 Incident lifecycle、显式 old-event upcaster、compatibility telemetry 和剩余旧入口 |
| M5 Domain Pack | 未开始 | 只从 Live Patch 与 Upgrade 已证明的共同 seam 抽取 contract/conformance suite |
| M6 证据驱动扩展 | 未开始 | 根据真实调用缺口决定 selector、受限动态计划或分布式化 |

当前分支的本地 Runtime 测试为 318 项全部通过，openUBMC Debug Runtime backend 为
16 项全部通过。正式 v2 仍需完成 M2/M3 的扩展 fault matrix、完整 execute A/B、release
gate，并在最终 source commit 上重新生成
`release-lock.json`；本分支不创建 v2 tag。

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
| [领域上下文](../CONTEXT.md) | 产品边界、统一术语、事实所有权和跨版本不变量 | 完成 |
| [架构决策记录](adr/README.md) | 四项难以逆转的已接受决策及其触发条件 | 完成 |
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

这组结果证明新的 Observation Interface 有效，但不能替代完整 `execute`、重启恢复和 Mutation fault injection。

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
- compatibility 与 operator profile 已分离；
- Observation scope、Receipt、Turn 和 Gate schema 已有输出预算；
- `execute` 支持 `start | respond | resume | control`；
- Observation A/B 已证明两个语义入口的方向正确；
- 当前真正风险不在工具数量，而在 compatibility 写入路径的收口、完整 execute
  fault matrix、配对 A/B 和正式 release lock 资格。

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
- `ContextRuntime` 继续作为兼容存储和底层执行 Adapter；Agent Gate response 当前仍通过
  `record_gate_submission -> phase_record` bridge 写入既有 Case events。它不再决定 Agent
  sequencing，但在 M4 完成前仍是待删除的过渡写入路径；
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
| Incident | 自动 reconcile 仍无法收敛时形成明确 Turn | 已实现基线 |
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

## 6. 后续四条主线

### 主线 A：发布安全与资格验证

这是当前第一优先级。未完成前不新增 selector 或 workflow family。

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
2. 用 compatibility Adapter 接住旧 operations；
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

### P0：v2 发布收口

| 目标 | 验收 |
| --- | --- |
| 输入与传输有界 | 超大输入在业务执行前拒绝；错误响应仍有界 |
| Observation handle 化 | Runtime 可从 handle/digest 重建；篡改、跨 target、GC 后 fail closed |
| Gate 持久身份 | 重复提交幂等；并发单赢家；旧版本、错误 Gate 和不同输入 conflict |
| Mutation 恢复证明 | Live Patch/Upgrade 每个切点不重复危险 Effect |
| 发布证据 | 当前 318 项 Runtime 与 16 项 Debug backend 测试通过；正式发布前仍需完整 execute fault matrix、A/B 与 release gate |
| ADR | 产品 Interface、状态权威、Effect、Gate、Artifact 和分布式触发条件落盘 |

### P1：v2.1 内部架构收敛

| 目标 | 验收 |
| --- | --- |
| typed semantic seam | Gateway 只依赖 `observe/execute` typed seam |
| 唯一 Run 状态权威 | `RunEngine` 是唯一 Run/Gate/Incident/Outcome 写入者 |
| 定义与执行分离 | `WorkflowDefinitions` 不执行 I/O 或写状态 |
| Domain locality | Adapter 预注册，execute/reconcile 经一个 DomainExecutor |
| Artifact 统一 | Observation、Evidence、build product、patch 和日志使用统一 ArtifactRef |
| 测试稳定 | contract tests 不依赖源码字符串和函数体切片 |

### P2：v2.x 按遥测扩展

1. Incident operator workflow；
2. workflow version migration 与 old-run support；
3. Artifact retention、ACL、redaction 和 GC；
4. selector 并行与连接复用；
5. D-Bus、active alarm、bounded log search 等按真实调用缺口增加；
6. compatibility profile 使用遥测与退役；
7. capacity、soak、property-based 和 network fault injection。
8. 原型验证 `ModelInvocationRecord` 与受限 `PlanProposal -> PlanRevision`，模型只生成 Proposal。

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

推荐默认答案：一次 command 只形成一次持久 Decision，包含 Run events、Gate/Incident 变化和 Effect intent；Domain Effect 的真实执行结果通过同一 effect identity 回到 RunEngine。需要进一步定稿 RunStore transaction 和旧 Case events 的 upcaster。

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

当前 `v2.0.0` 尚未正式发布，因此本分支先完成 typed RunEngine 的候选实现，但不打 tag、
不更新 release lock。只有 fault matrix、完整 execute A/B 和 release gate 在最终 source
commit 上通过后，才重新生成发布锁；否则继续作为后续候选分支演进。

## 9. 需要持续验证的假设

1. ObservationRef 会显著降低完整 execute 的 cached-input；
2. Turn-to-next-Gate 在 30–120 秒 Effect 下仍能避免额外模型调用；
3. typed Port 与 Adapter 预注册会降低变更影响面和测试设置成本；
4. 单进程 Runtime 足以满足 v2.x 的并发、存储和恢复 SLO；
5. Case 暂不独立建模不会阻碍多阶段 Run；
6. Incident 正式化能改善 unknown recovery，而不会形成新的 Operator 工具膨胀；
7. Skill 渐进披露与阶段上下文隔离能显著降低完整 Build-Upgrade Token；
8. compatibility profile 可以在有遥测和回滚窗口后安全退役。

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
