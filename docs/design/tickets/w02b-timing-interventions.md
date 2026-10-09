# W02b：重启时间与人工介入事实投影

规格：[Run/Task measurements v2](../run-measurements-v2.md)。
依赖：W02a 的可信读取/版本选择/失败隔离。

## 行为

v2 handoff 区分包含等待和重启间隔的 Host wall_seconds 与完整 monotonic 分段
形成的 active_seconds。Task 使用自己的观察区间，避免把多个并行 Run 的耗时
相加。Host 确认的 approval/decision/repair 事件按 event identity 去重；Gate
或自动重试不产生人工计数。

## 实现范围

在 W02a 的单份 task-scoped measurement snapshot 中启用 wall interval、timing
segment、intervention event 验证及纯投影。复用原 producer 的时间和人类事件
来源；没有 clock continuity、完整分段或 actor identity 时输出 null。

模型用量、Runtime Outcome 和 terminal delivery 的合同不由这些统计扩展改变。
operation/test 证据扩展另行定义，因为本切片的计量来源不能证明官方测试或设备
验收完成。

## 验证

- 同一 clock_ref 30s wall 与跨重启 4s/6s 分段得到 wall=30、active=10；重复
  segment 不双算，缺段/回拨/缺终点/epoch 改变保留相应 null。
- A/B Run 在同一个 Task interval 内并行，Task wall 使用自己的 30s 区间。
- 一个重复 approval、两条独立 decision、自动重试产生 count=3；完整空来源
  为 0；旧 Gate、未确认 actor 或缺完整事件来源为 null。
- Event/segment 同身份冲突及来源读取失败只降低计量可用性；恢复后读取同一
  来源得到相同计数，已完成 Effect 不重做，prepared 不提升为 delivered。

验证通过可信 source adapter→handoff 和既有 execute/capture/restart 两个公开接口。
