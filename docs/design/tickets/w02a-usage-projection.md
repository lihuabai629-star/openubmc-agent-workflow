# W02a：可信计量读取与去重用量投影

规格：[Run/Task measurements v2](../run-measurements-v2.md)。
依赖：W01 本地基线 `a21f4f0`。

## 行为

同一 Task 的 A/B 两个 Run 使用 Host 注入的单次快照读取。v2 handoff 对稳定
provider invocation identity 去重，保留 Run/Task 归属、observed/synthetic 来源与
coverage。完整且已观察到的零为 0，未知为 null；Task 汇总不相加 cached/input，
不重复计算同一调用。默认 v1 消费者保持 W01 行为。

## 实现范围

先定义允许字段及冻结/验证，再接入 HostContinuity。source adapter 将现有已保存
的 provider 观察转换为规范化快照；缺身份/归属的旧报告保持 unavailable。adapter
不拥有 provider 调用、价格、Run 状态或来源保留策略。输入中的时间与人工列表在
此切片只接受空列表及 unavailable coverage；由 W02b 实现实际投影。

如现有来源不能提供确切调用 identity，只交付明确降级行为及可替换的 reader
接入；不能将合成 fixture 称为已安装采集能力。

## 验证

- 公开 source adapter→v2 handoff，以 100/20/40 与 50/10/null 的独立数值预期
  验证重复、重试、部分计量及 Task-only attribution。
- 同身份冲突、错误 Task/Run、未知字段、digest 不一致、布尔/负数 token、
  cached 大于 input、超限快照拒绝；计量 unavailable 不改变 Runtime 结果。
- complete 空集合得到 0；没有 reader、缺 usage、缺 inventory 时得到 null。
- 既有 execute/capture/restart seam 验证 v1 默认、v2 opt-in、A/B 来源固定、
  恢复不 dispatch 替代 Effect、fresh readback 与计量失效各自显示可用性。
