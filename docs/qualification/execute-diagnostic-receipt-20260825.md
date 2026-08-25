# Execute DiagnosticReceipt 真实只读资格验证（2026-08-25）

## 结论

最终候选提交 `036253c7fc6ced9f0cdb94791f85f6a0604f6d28` 完成了
`execute(debug_run)` 的端到端诊断闭环。六个真实只读场景全部在一次
`observe` 或 `execute` 调用内返回实质证据，场景目标 6/6 满足，
false-success 数为 0，不需要恢复第三个 Agent-facing Evidence 读取工具。

Runtime 持久化 `DiagnosticReceipt`，Turn 直接投影 source/visible coverage、
freshness、capability、gaps、Evidence refs 和可判断的 `value` / `summary`。
投影压缩只改变 Agent 当前可见程度，不再改写源证据的 status 或
coverage。结构化诊断还显式记账 `target-clock`；未返回时会形成
`not_checked` 和 gap，不再静默缩小 requested scope。

模型对五个 `execute` 场景最终均输出 `partial`，这是因为它对压缩投影
不支持的完整负面结论保持保守，不等于 Runtime 源回执没有闭环。S1–S4
的源 `DiagnosticReceipt` 全部是 complete；S6 因源日志刻意截断而正确保持
partial。

Closeout 与 Replay 还使用独立的 Agent acceptance 分类：源回执 complete
但 durable 可见 coverage 只有部分可评价时为 partial，零可见可评价结果时为
blocked，同时不改写源 `status/coverage.complete`。

## 验证边界

| 项目 | 值 |
| --- | --- |
| Source | `036253c7fc6ced9f0cdb94791f85f6a0604f6d28` |
| Agent Interface | `observe`、`execute` |
| 模型 | `gpt-5.6-sol` |
| 目标 | `BMC-T1` |
| 场景 | 6 个真实只读场景 |
| 调用 | 5 次 `execute`、1 次 `observe` |
| 写保护 | 本地只读 MCP 代理拒绝所有非只读调用 |

目标地址、凭据路径和值、硬件地址均已脱敏。原始未脱敏事件仅位于内存文件系统，
运行结束后已删除；仓库只保留本报告和紧凑指标。

## 场景结果

| 场景 | Runtime 源状态 | 目标 | 结果 |
| --- | --- | --- | --- |
| S1 身份、版本、运行状态和能力 | complete | 通过 | 版本、uptime、SSH/Telnet/MDBCTL/BUSCTL、服务、MDB 和 `target-clock` 前后采样可见，时钟正常前进 7 秒。 |
| S2 MCTP timeout | complete | 通过 | 可见 mctpd request-timeout 日志、时间和服务上下文；未臆测根因、持续性或恢复状态。 |
| S3 storage / hwproxy | complete | 通过 | 可见 storage 对象树、Drive Name/Presence/Health 原始值和 storage 请求 hwproxy 超时；未把裸值提升为故障语义。 |
| S4 devmon / shmlock | complete | 通过 | 可见 devmon 与 repeat acquire/release 时间线；正确拒绝持锁者、等待链、死锁和根因断言。 |
| S5 窄范围 MDB | Observation complete | 通过 | 一次 `observe` 返回 Name/Presence/Health 原始值、4/4 coverage、coherent consistency 和 live freshness。 |
| S6 有界截断时间线 | partial（预期） | 通过 | 返回可见事件顺序，同时明确 `truncated=true`、`content_complete=false` 并拒绝完整负面结论。 |

S3 和 S6 的 `projection_target_exceeded=true` 没有成为控制流硬阻塞；S3 的源证据
仍是 complete，S6 则只因源内容截断而 partial。这直接验证了 8 KiB Turn
作为软投影目标的语义。

## 成本与投影

| 指标 | 19 工具旧版 | 修复前两工具版 | 最终候选 |
| --- | ---: | ---: | ---: |
| Total tokens | 1,110,135 | 378,431 | 365,903 |
| 非缓存输入 + 输出 tokens | 383,607 | 116,287 | 143,695 |
| MCP events | 21 | 6 | 6 |
| 累计 wall time | 570.381 s | 365.999 s | 355.602 s |
| Agent 可见工具输出 | 5,999,095 B | 8,336 B | 60,431 B |
| 压缩后持久化 Evidence | 161,596 B | 160,737 B | 161,425 B |

相对 19 工具旧版，最终候选减少 67.04% Total tokens、62.54% 非缓存输入与输出、
71.43% MCP events、37.66% wall time 和 98.99% Agent 可见工具输出，实质证据场景从
4/6 提升到 6/6。持久化 Evidence 减少 0.11%，基本不变。

相对修复前两工具版，Total tokens 减少 3.31%，wall time 减少 2.84%；非缓存
Token 增加 23.57%，Agent 可见输出增加 624.94%，持久化 Evidence 增加 0.43%。
可见输出增长是恢复有界诊断回执的预期代价，仍比 19 工具版少 98.99%。
单次真实模型运行不足以独立证明长期性能趋势。

## 资格判断

- 一个 Runtime Core、`observe` / `execute` 两个语义入口的方向成立。
- `execute` 的 operation completion 与诊断完成已经分离；零实质结果不会自动形成成功诊断。
- source coverage 与 visible coverage 已经分离；投影压缩不改写源 `status/coverage.complete`。
- Closeout 只在 durable visible coverage 完整可评价时通过；部分可评价为 partial，零可评价为 blocked。
- Gate 继续使用 4 KiB 硬契约；8 KiB Turn 是显示软目标，不阻断 Gate、Incident、Outcome 或 DiagnosticReceipt。
- 源内容截断会稳定传播 partial、gaps 和不支持的断言；投影压缩本身不会触发这些结论。
- 原始 Evidence 继续留在 Runtime / Operator 面；Agent 通过有界回执完成诊断，不增加 Broker、Worker fleet 或分布式执行层。

机器可复核摘要位于 [execute-diagnostic-receipt-20260825.json](execute-diagnostic-receipt-20260825.json)。
