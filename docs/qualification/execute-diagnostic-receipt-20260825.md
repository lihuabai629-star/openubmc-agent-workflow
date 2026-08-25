# Execute DiagnosticReceipt 真实只读资格验证（2026-08-25）

## 结论

候选提交 `01d25d3fa9a722d1eee8cd808db19e5fb9508092` 恢复了 `execute(debug_run)` 的端到端诊断闭环。六个真实只读场景全部在一次 `observe` 或 `execute` 调用内返回实质证据，没有出现 generic completion 冒充诊断成功，也不需要恢复第三个 Agent-facing Evidence 读取工具。

修复后的关键变化是：Runtime 持久化 `DiagnosticReceipt`，Turn 直接投影 coverage、freshness、capability、gaps、Evidence refs 和可判断的 `value` / `summary`。日志摘要经历持久化压缩和 Agent 显示压缩后仍保留实际命中或完整零命中结论；`projection_truncated` 只表示显示压缩，不再改变源证据完整性。

Issue 验收通过。剩余功能缺口是身份场景没有采集目标时钟，因此 S1 保持 partial；这不是 `execute` Evidence 闭环回归。

## 验证边界

| 项目 | 值 |
| --- | --- |
| Source | `01d25d3fa9a722d1eee8cd808db19e5fb9508092` |
| Agent Interface | `observe`、`execute` |
| 模型 | `gpt-5.6-sol` |
| 目标 | `BMC-T1` |
| 场景 | 6 个真实只读场景 |
| 调用 | 5 次 `execute`、1 次 `observe` |
| 写保护 | 本地只读 MCP 代理拒绝所有非只读调用 |

目标地址、凭据路径和值、硬件地址均已脱敏。原始未脱敏事件位于内存文件系统，并在运行结束后删除；仓库只保留本报告和紧凑指标。

## 场景结果

| 场景 | 最终状态 | 结果 |
| --- | --- | --- |
| S1 身份、版本、运行状态和能力 | partial | 部署版本、uptime、SSH/Telnet/MDBCTL/BUSCTL、MDB 和 D-Bus 证据可见；目标时钟未采集。 |
| S2 MCTP timeout | completed | 可见 mctpd request-timeout 日志、时间和服务上下文；未臆测根因。 |
| S3 storage / hwproxy | completed | 可见 storage 对象树、Drive Name/Presence/Health，以及 storage 请求 hwproxy 超时日志；未把它提升为服务整体不可用或 Drive 故障。 |
| S4 devmon / shmlock | partial | 可见 devmon 与 repeat acquire/release 日志；正确拒绝持锁者、等待链、死锁和根因断言。 |
| S5 窄范围 MDB | completed | 一次 `observe` 返回三项原始值、4/4 coverage、coherent consistency 和 live freshness。 |
| S6 有界截断时间线 | partial | 返回可见事件顺序，同时明确 `truncated=true`、`content_complete=false` 和无法支持的负面结论。 |

六个场景都有实质可引用证据；五个完整满足场景目标，S1 因目标时钟缺失而部分满足。S4 和 S6 的 partial 是正确的保守状态，不是流程未闭环。false-success 数为 0。

## 成本与投影

| 指标 | 19 工具旧版 | 修复前两工具版 | 当前候选 |
| --- | ---: | ---: | ---: |
| Total tokens | 1,110,135 | 378,431 | 355,156 |
| 非缓存输入 + 输出 tokens | 383,607 | 116,287 | 144,724 |
| MCP events | 21 | 6 | 6 |
| 累计 wall time | 570.381 s | 365.999 s | 524.241 s |
| Agent 可见工具输出 | 5,999,095 B | 8,336 B | 42,527 B |
| 压缩后持久化 Evidence | 161,596 B | 160,737 B | 161,135 B |

相对 19 工具旧版，当前候选减少 68.01% Total tokens、71.43% MCP events 和 99.29% Agent 可见工具输出，同时把实质证据场景从旧版 4/6 提升到 6/6。

相对修复前两工具版，持久化 Evidence 仅增加 0.25%，Agent 可见输出增加到约 5.1 倍，这是恢复诊断可见性的预期代价；Total tokens 下降 6.15%。非缓存 Token 和 wall time 分别增加 24.45% 和 43.24%，单轮真实模型运行波动较大，后续应通过重复资格样本观察趋势，不能据此宣称性能回归或收益。

## 资格判断

- 一个 Runtime Core、`observe` / `execute` 两个语义入口的方向成立。
- `execute` 的 operation completion 与诊断完成已经分离；零实质结果不会自动形成成功诊断。
- Gate 继续使用 4 KiB 硬契约；8 KiB Turn 是显示软目标，不阻断 Gate、Incident、Outcome 或 DiagnosticReceipt。
- 原始 Evidence 继续留在 Runtime / Operator 面；Agent 通过有界回执完成诊断，不增加 Broker、Worker fleet 或分布式执行层。
- 下一阶段优先补齐目标时钟等明确只读 selector，并持续验证重复样本的延迟和非缓存 Token；不需要恢复 19 个顶层工具。

机器可复核摘要位于 [execute-diagnostic-receipt-20260825.json](execute-diagnostic-receipt-20260825.json)。
