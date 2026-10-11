# Run 与 Task 计量记录 v2

源码基线：`a21f4f08049bcf1146af3a02b16fdd8e9b4e48f8`。
现有 v1 合同：[Run records](../run-workspace-records.md)。

## 接入方式与事实归属

HostContinuity 通过两个可选构造参数接入计量：

```python
HostContinuity(
    root,
    measurement_reader=None,
    record_schema_version=1,
)
```

`measurement_reader(task_id, run_refs) -> Mapping | None` 由可信 Host composition
注入。`run_refs` 是当前 Task 书签的唯一 Run ID tuple；reader 返回该范围的一份
完整读取快照，而不是每个 Run 分别读取变化中的全局来源。每次 v2 handoff 读取
一次，先验证、深拷贝、规范化，再用于所有 Run 记录和 Task 汇总。

现有采集脚本及 Host adapter 继续拥有原始计量事实；RunEngine 继续拥有 Run、Gate、
Effect 和 Outcome。投影器不建立计量数据库。恢复后重新读取原来源；来源失效或
缺失时显示 unavailable，不缓存上一次已知数字。已安装 Host 的 reader 注册及
其他项目的采集实现另行接入；本合同提供 Workflow 编程接口与离线 source adapter。

reader 的输入来自实际 Host Task/书签；Agent action、MCP `_meta` 和 notes 不能
设置 reader、计量值或覆盖范围。读取计量不会发起 provider 请求或调用设备。

### 已保存来源的 reader

`JsonMeasurementReader(path)` 读取 producer 已保存的规范化快照。每次重新打开文件，
最多读取 512 KiB；重复 JSON object key、超限、非法 JSON 或文件丢失使计量
unavailable。该 reader 不创建文件、不保存缓存，也不控制来源的保留期限。

`ProviderReportReader(path, task_id=..., provider_ref=..., evidence_kind=...,
inventory_complete=False)` 适配现有 `runtime_model_measurement.py` 报告中的
`provider_requests`。Host 绑定确切 Task/provider，并显式声明 inventory 是否完整。
每条原观察须有 producer 保存的 `invocation_ref`；Run 归属使用原 `run_ref`，
缺少该键时只计 Task，且不宣称 Run inventory 完整。原观察缺稳定 identity 时，
adapter 降低覆盖度，不根据数组位置或 response id 补造调用身份。

adapter 读取原 usage 的 input/output 和 `input_tokens_details.cached_tokens`
（或一致的 `cached_tokens`），只保留允许字段。缓存别名一方为 null 时使用另一方
的已知值；两方均非 null 时必须一致。原来源声明的 Task/provider 与
Host 绑定不一致时拒绝。已有未带 invocation identity 的历史报告仍显示
unavailable；该 reader 不修改历史文件或启用已安装采集器。

## v1 与 v2

默认 `record_schema_version=1`，保留 W01 字段及 unavailable/null 用量，也不调用
reader。可信 Host 显式选择 `2` 后，Run record 与 Task aggregate 的
`schema_version` 为 2；已有身份、工作区绑定、workflow、Runtime 状态及 Outcome
引用语义保留。未知 schema version 在 Host 构造时拒绝。

v2 增加 `measurement_source`、`timing`、`interventions`，并扩展 `usage` /
`usage_totals`。HostContinuity 外层的 `openubmc.host-continuity/v1` envelope
保持其现有含义，内部记录按各自 `schema_version` 解码。

## 规范化来源快照

外层仅接受下列字段：

| 字段 | 类型与含义 |
| --- | --- |
| `schema_version` | Integer 1，计量输入格式版本，与输出记录版本分开 |
| `task_ref` | 与 Host 调用的 Task identity 完全相同 |
| `source_ref` | `sha256:<64 lowercase hex>`；对除自身外的规范化 allowlist 数据重算 |
| `evidence_kind` | `observed` 或 `synthetic`；后者在所有输出中保留 |
| `task_coverage` | `usage` / `timing` / `interventions` 三个 coverage 值 |
| `run_coverage` | 按 `run_ref` 唯一的条目，每条含上述三个 coverage 值 |
| `invocations` | 模型调用事实列表 |
| `timing_segments` | Host 单进程计时段列表 |
| `wall_intervals` | Task/Run 的 Host 起止观察事实列表 |
| `intervention_events` | Host 确认的人类事件列表 |

coverage 为 `complete`、`partial` 或 `unavailable`。它描述这份快照的观察范围，
不表示 Task 已完成。`run_coverage` 缺失某个书签时，该 Run 的三个 coverage 均为
unavailable；来源中的非空 Run reference 必须属于传入的书签范围。

引用复用 W01 的非秘密 opaque reference 格式。每份快照最多 256 个 invocation、
256 个 timing segment、129 个 wall interval、256 个 intervention event，以及
128 个 Run coverage；同时满足现有 bounded-request 节点、深度、容器及字符串预算。
超限不截断为完整统计，返回计量 unavailable。未来更大来源可另行定义分页快照合同。

允许字段之外的数据一律拒绝。输入中不含绝对路径、endpoint、credential、原始
prompt、response、日志或自由 notes。digest 检查只证明内容一致；可信来源来自
Host composition，不能由 digest 自报证明。

## 模型调用与归属

每个 invocation 仅含：

| 字段 | 含义 |
| --- | --- |
| `provider_ref`、`invocation_ref` | 共同构成一次物理 provider 调用的稳定身份 |
| `run_ref` | 一个确切 Run ID 或 null（仅能归属于 Task） |
| `model_ref`、`reasoning_ref` | 实际观察到的 opaque identity 或 null |
| `input_tokens`、`output_tokens`、`cached_tokens` | 非负 integer 或 null；bool 拒绝 |
| `source_ref` | 原来源的非秘密、稳定证据引用 |

`input_tokens` 使用 provider 报告的总输入量；`cached_tokens` 是其中的缓存输入
子集，不再次加到 input。缓存计量口径不明的来源保留 null，不猜测转换关系。
两者都已知时，cached 不得大于 input。

同一 `(provider_ref, invocation_ref)` 的相同重复事实只计一次。相同身份的任意
字段冲突使该快照 unavailable，既不挑较大数字，也不把冲突当成新调用。实际重试
如果发起了另一条 provider 请求，必须有另一个 invocation identity，并分别计量。
重复读取、重放同一完成事件、重复书签都不扩大总数。

Task 汇总从唯一 invocation 集合计算，而不是相加 Run 的预计算总量。Task-only
调用不分摊给任何 Run，单独报告 `unattributed_invocation_count`。缺乏稳定身份或
确切归属的旧 session 总量不能按行号、数组位置、时间邻近或内容 hash 伪装成
provider invocation；由 adapter 标记覆盖不足。Task 级已知总量可以另行保留在原
来源中，本切片不把它拆分为 Run 数字。

### 用量输出

`usage` / `usage_totals` 的字段为：

```json
{
  "status": "unavailable",
  "input_tokens": null,
  "output_tokens": null,
  "cached_tokens": null,
  "invocation_count": null,
  "source_ref": null
}
```

Task 的 `usage_totals` 另含 `unattributed_invocation_count`。

每个数字独立计算：只有相应 scope 的 inventory complete，且所有唯一调用都提供
该值时，才输出总数；否则该数字为 null。inventory complete 时 invocation_count
可以是确切计数。coverage complete 且明确没有任何调用时输出观测零；单个调用
缺 usage 时仍保留 invocation_count，但不能用零补 token。
总数超出当前 JSON encoder 的整数表示限制时，仅该字段为 null；其余独立可表示
的数字与合法 source_ref 保留，不放宽解释器的整数转换限制。

有合法来源、至少一项已知数字，但其他必要数字缺失时 status 为 partial；全部
数字已知时为 available；没有可用数字时为 unavailable。合法来源的 source_ref
仍保留。partial 不是已知子集求和；它允许不同字段的完整度不同。

## 时间口径

`wall_intervals` 的条目仅含 `interval_ref`、`run_ref`（Task interval 为 null）、
`started_at`、`ended_at`、`clock_ref`、`source_ref`。起止为有 UTC 时区的 RFC3339
时间或 null（小数秒最多六位），同一 scope 最多一个 interval。clock_ref 为
连续可信时钟的 reference；连续性未知或 epoch 改变时为 null。它表示 Host 对该 Task/Run 的起止
观察，不能冒充精确的 RunDecision commit timestamp。

`wall_seconds` 为同一可信 `clock_ref` 下起止观察之差，包含 Gate 等待、Host 离线
和重启间隔。只有终点已观察到、该 scope 的 timing coverage complete、时钟连续
且终点不早于起点时，才输出完整值。进行中的 Run 终点为 null；不随 handoff
读取时刻虚构终点。时钟发生回拨或 epoch 改变时完整 wall 值为 null。

`timing_segments` 条目仅含 `segment_ref`、`run_ref`、`clock_ref`、
`elapsed_seconds`、`source_ref`。elapsed 是 producer 在同一进程/clock epoch
内部测量的有限、非负 monotonic 秒数；跨进程 monotonic 起止值不能直接相减。
相同 segment_ref 重复去重，字段冲突拒绝。只有覆盖完整，才能将分段秒数相加为
完整 `active_seconds`；缺段或总和超出可表示的有限秒数时保留 null。
来源必须在同一 scope 保证分段互不重叠；
该字段表示 Host 测量运行段，可能包含该段内部的等待，并非 CPU 时间。

输出 `timing` 为 `status`、`started_at`、`ended_at`、`wall_seconds`、
`active_seconds`、`source_ref`。partial/unavailable 的数字规则与用量相同。
Task 时间使用 Task 自己的 interval/segment；Run 时间不能相加为 Task 墙钟时间，
也不能将并行 Run 的 active_seconds 相加冒充 Task active_seconds。

## 人工介入

event 仅含 `event_ref`、`run_ref`（Task-only 可为 null）、`actor_kind=human`、
`kind`、`gate_ref`、`source_ref`。kind 为：

- `approval`：Host 确认的人类授权或批准回应。
- `decision`：人类提交了影响下一步工作的选择或 Gate 输入。
- `repair`：Host 观察到人类完成了恢复工作所需的修复。

gate_ref 为实际 Gate 的 opaque reference 或 null。Host 对一个物理人类事件只赋予
一个 event_ref 和一个分类；同一回应既含批准又含选择时以 approval 分类，不再
生成 decision。重复递交/回读同一事件只计一次，冲突身份拒绝。自动模型回应、
自动重试、工具调用、普通状态提问和未被 Host 确认的自由文本不产生事件。

现有 RunEngine Gate/Incident 数量不能推断 human_interventions；没有 actor 证据
时保留 unavailable。输出为 `status`、`count`、`counts_by_kind`（上述三个键）、
`source_ref`。coverage complete 才输出完整 count；完整空集合为观测零，缺失
来源为 null。Task 对 event_ref 去重，不通过 Run 计数求和。

## 失败与一致性

`measurement_source` 为 `status`、`source_ref`、`evidence_kind`。合法快照提供
available/source_ref/kind；None、读取失败、未知字段、冲突身份、错误绑定或 digest
不一致提供 unavailable/null/null。错误不回显来源值或异常消息。

计量读取/验证/投影失败仅降低计量字段的可用性。已提交的 execute 结果、Run binding、
Runtime 状态、Outcome 和 terminal preparation/delivery 不变；不得据此重做模型
或设备操作。Runtime fresh readback 不可用时仍保留 Run identity，并清空 W01 的
权威 Runtime 字段；独立合法的计量事实可保留，但不能证明 Runtime 完成。

v2 handoff 的 Runtime readback 与计量源读取不是跨存储事务；输出是两个分别标明
来源的事实读取，不承诺全局同一时刻。对同一规范化快照与同一 Runtime 投影，
所有汇总及引用按稳定排序输出。来源文件的落盘、恢复和保留仍由原 producer 管理。

费用/价格、operation/test 证据扩展、导出及 retention 采用独立后续合同；本合同
没有足够的价格或 test provenance 字段来计算这些结论。

## 公开接口验证场景

1. 可信 source adapter → HostContinuity v2 handoff：验证规范化、稳定身份、
   归属、coverage、冲突、观测零/未知及 Task 去重；不能证明真实 provider 采集。
2. 既有 execute → capture → fresh handoff/restart：使用合成 Host/Domain adapter，
   验证 A/B 绑定、已有 Run 不重执行、计量失败隔离、v1 默认兼容与 terminal 状态；
   不能证明已安装 Host、真实模型/设备或跨机器时钟同步。

具体独立预期：A 的调用为 100 input / 20 output / 40 cached，B 为
50 / 10 / null；重复 A 调用不增加数字。Task 为 150 input / 30 output /
null cached，unique invocation_count 为 2，status partial。新增一次真实重试
（新 invocation_ref）为 30 / 5 / 0 后，Task 为 180 / 35 / null，计数为 3。
cached 不再次加入 input。

时间例子：同一 clock_ref 的 UTC 10:00:00→10:00:30，以及重启前后的 4s、6s
独立完整分段，wall=30s、active=10s。完整度不足时 active=null；Run 等待或终点
缺失时 wall=null。两个 Run 共享一个 Task interval 时，Task wall 仍为 30s。

人工例子：一个 approval 被重复读取，两条独立 decision、一条自动重试，完整
人工事实输出 count=3，approval=1、decision=2、repair=0。旧 Gate 记录未证实
actor 时 count=null。
