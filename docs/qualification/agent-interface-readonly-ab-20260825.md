# Agent Interface 只读 A/B 验证（2026-08-25）

## 结论

`observe` 证明“两入口 Agent Interface + Runtime Core”方向成立：在窄范围 MDB 场景中，新接口以一次调用返回正确值、覆盖率、一致性、freshness 和 gaps，结论质量优于旧接口，成本显著下降。

`execute(debug_run)` 的当时版本存在阻断性回归。Runtime 持久化的压缩 Evidence 只减少 0.53%，但 Agent 可见工具输出减少 99.86%；Turn 只显示通用完成摘要和 Evidence ID，Agent 无法评价已经采集的日志、服务树、MDB、版本和 uptime。部分场景仍把 `stage.diagnosis` 标为 passed，形成接口层 false-success 风险。

因此，产品方向无需回退到 19 个 Agent-facing 工具。问题位于 Runtime 的 Turn 投影和完成判定，而不是两入口架构本身。

本报告是修复前基线：新版测试对象为 `main@3d8cf49bc3d0`，不包含后续
`DiagnosticReceipt` 候选实现。候选分支只有在相同六个真实只读场景重新运行后，
才能证明 `execute` 的端到端诊断闭环已经恢复。

## 基线

| 项目 | 旧版 | 新版 |
| --- | --- | --- |
| Source | `v1.2.2` / `35b36efb6503` | `main` / `3d8cf49bc3d0` |
| Agent 可见工具 | 19 | 2（`observe`、`execute`） |
| 场景 | 6 个真实只读场景 | 同一组场景 |
| 目标 | `BMC-T1` | `BMC-T1` |

测试使用隔离 checkout、隔离状态目录和只读代理。目标地址、凭据路径和值、硬件地址均未进入本报告；未脱敏临时数据在验证后删除。

## 汇总指标

| 指标 | 旧版 | 新版 | 变化 |
| --- | ---: | ---: | ---: |
| Total tokens | 1,110,135 | 378,431 | −65.91% |
| 非缓存输入 + 输出 tokens | 383,607 | 116,287 | −69.69% |
| MCP events | 21 | 6 | −71.43% |
| 累计 wall time | 570.381 s | 365.999 s | −35.83% |
| Agent 可见工具输出 | 5,999,095 B | 8,336 B | −99.86% |
| 压缩后持久化 Evidence | 161,596 B | 160,737 B | −0.53% |
| 解压后持久化 Evidence | 1,781,709 B | 1,780,521 B | −0.07% |

旧版进行了 15 次 Evidence 读取，返回 5,657,083 bytes；按同一 Evidence 的读取区间合并后，4,385,122 bytes（77.52%）属于重复或重叠读取。新版没有这类成本，但当时也没有在 Turn 内提供足够的紧凑投影。

## 场景判定

| 场景 | 旧版 | 新版（当时） | 判断 |
| --- | --- | --- | --- |
| 身份、版本、运行状态和能力 | 有实质结果 | Evidence 隐藏 | 回归 |
| MCTP timeout | Evidence 续读失败 | Evidence 隐藏 | 两端均未完成 |
| storage / hwproxy | 正确且有实证 | Evidence 隐藏 | 明显回归 |
| devmon / shmlock | 正确且有实证 | Evidence 隐藏且验收过于乐观 | 阻断性回归 |
| 窄范围 MDB | 正确但两次调用 | 单次完整 ObservationReceipt | 明确改进 |
| 有界截断时间线 | Evidence 续读失败 | 截断元数据隐藏且验收过于乐观 | 两端均未完成 |

实质完成数为旧版 4/6、新版 1/6。两版都没有把 recurring timeout、Drive 裸值或 shmlock 日志直接解释为唯一根因。

## 架构含义

下一阶段的优化重点应保持在 Runtime Core 内：

1. `execute` Turn 必须携带有界、可引用的诊断回执，而不是让 Agent 续读原始 Evidence。
2. 阶段完成与诊断完成必须分离。零可评价结果应阻断；只有部分结果可评价时才是 partial。
3. coverage、freshness、truncation、content completeness、capability 状态、gaps 和 Evidence refs 必须共同进入 Agent 可见投影。
4. `observe` 后续可扩展到更多明确只读 selector，但不应以恢复大量顶层工具为代价。
5. Replay/Golden 应长期锁定 false-success、Evidence 可见性和截断传播语义。

精简、可机器复核的指标位于 [agent-interface-readonly-ab-20260825.json](agent-interface-readonly-ab-20260825.json)。
