# W02 计量来源盘点

盘点源码：`a21f4f08049bcf1146af3a02b16fdd8e9b4e48f8`。

| 当前来源 | 已有事实 | 直接复用的限制 | v2 adapter 处理 |
| --- | --- | --- | --- |
| `scripts/runtime_model_measurement.py:66` | 物理请求的 started_at、first-byte、duration、requested/response model、usage | relay row 缺稳定 provider invocation ID 和单个 Run 归属；数据先留在进程 records | 原 producer 生成并持久保存 invocation identity；确切 Host attribution 缺失时只归 Task；没有恢复来源时 unavailable |
| `scripts/stateful_agent_evaluation.py:167` | CLI turn usage 与 native cumulative usage，缺失 token 为 None | 累计 session 总数不是逐 invocation 事实；native tool call 计数也不是 model invocation 数 | 保留原统计，只有真实 invocation identity/归属完整的来源才能输出 v2 调用项；不得相加 cumulative 与 streamed 两份计量 |
| `scripts/stateful_agent_evaluation.py:795` | 单个 trial 的 monotonic elapsed_seconds | trial 与 Run 粒度不同；另一个进程的 monotonic 起点没有共同基准 | 作为确切 trial/Task 的观察段；没有明确 Run 映射就不填 Run 时间；synthetic 标识保留 |
| `scripts/plugin_task_evaluation.py:121` | wall_seconds、cost_usd、extra_tool_calls、human_interventions、recovery_attempts 字段 | human count 可来自独立 review；不是带身份的事件流；wall 可来自 harness budget | 无 event identity 的旧人工总数不拆成事件；已有 review 不改写实际 harness 已测值；费用保留在原报告 |
| `openubmc-target-runtime/openubmc_target_runtime/run_engine.py:1961` | phase/operation、Gate identity、submission identity、recorded_at 与 workflow binding | Gate 回应可以来自模型/自动化；recorded_at 不是 monotonic 用时 | phase/test 扩展使用既有 Runtime provenance；不能推断 human actor 或计时；不直接读取 SQLite 来建立新事实 |
| `openubmc-target-runtime/openubmc_target_runtime/tracing.py:22` | 有预算、可丢弃的 span 观察 | 队列可丢弃、每 Run 有上限、引用为进程 HMAC；不是完整采集源 | 不作为 token、完整时间或人工介入的权威来源 |
| `openubmc-target-runtime/openubmc_target_runtime/run_record.py:8` | v1 usage 明确 unavailable/null | 无 collector/source input，不能读取当前 session 总量填旧 Run | 保留 v1；v2 显式注入 reader，只投影可验证归属的来源 |
| `openubmc-target-runtime/openubmc_target_runtime/host_continuity.py:246` | Task→unique Runs、fresh Runtime readback、terminal prepare/delivery | 书签更新时间不是 Run 起止，notes 不具备事实权威 | 在这条 handoff seam 读取一份 task-scoped measurement snapshot，保持既有 Run/delivery 语义 |

这些脚本已经解决不同粒度的采集或报告，尚未提供统一的稳定 invocation、coverage
和人类 event 合同。v2 adapter 复用事实及来源；新增的部分集中在身份、粒度、完整性
和投影。恢复读取能力取决于原 producer 是否已有可读的持久来源。

已有测试位于 `scripts/tests/test_runtime_measurement.py`、
`scripts/tests/test_stateful_agent_evaluation.py`、
`scripts/tests/test_plugin_task_evaluation.py`、
`openubmc-target-runtime/tests/test_workspace_run_record.py` 与
`openubmc-target-runtime/tests/test_host_continuity.py`。后续修改只选择涉及实际改变
的公开接口回归；v2 场景中的期望值由规格的数值例子给出。
