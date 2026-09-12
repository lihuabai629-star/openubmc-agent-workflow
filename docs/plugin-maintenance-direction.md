# 插件维护与能力方向

最近已关闭的问题集中在安装状态与实际使用状态不一致：[#232](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/232) 是运行缓存触发完整性误报，[#237](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/237) 是升级后仍使用旧启动覆盖，[#229](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/229) 是示例修改了不可变安装目录。维护优先级因此落在安装、更新和恢复过程，而不是扩展 Agent 编排层。

本机页面显示安装文件、用户级启动配置、Runtime 与 KB 的独立状态。用户可以预览标准启动覆盖的清理结果，备份后应用，并在没有后续配置修改时撤销。自定义包装器、环境变量和认证策略保留原值并报告冲突，避免把“修复”变成丢弃本地设置。

## 验证范围

| 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 文件与依赖校验 | 所选安装内容符合锁定身份 | 桌面实际使用了该入口 |
| Runtime／KB 启动检查 | 两个本机服务能够初始化 | BMC 或知识库远端认证成功 |
| 原生安装、卸载、重装和中断恢复测试 | 客户端配置及安装事务能恢复并保留外部配置 | 每种桌面客户端都没有恢复问题 |
| 持久任务的原生 `thread/resume` | 独立 app-server 能恢复原任务 ID | 桌面 UI 日志出现恢复成功 |
| 桌面任务恢复日志 | 对应客户端、任务和时刻的实际恢复结果 | 其他版本与平台自动获得相同资格 |

2026-09-11 的用户回报确认受影响桌面任务恢复成功；自动资格报告单独保存协议测试结果，不将该历史回报提升为新版本的桌面验收。

## 能力投入顺序

1. **安装与恢复可靠性。** 将缓存变化、旧启动入口、依赖中断和配置并发修改纳入维护回归。出现真实失败时，记录版本、错误分类、发生阶段和恢复结果，秘密值留在本机。
2. **配置到请求的一致性。** [#222](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/222)、[#223](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/223)、[#227](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/227) 说明，仅保存配置不足以证明请求使用了它。继续通过已激活版本及请求使用的凭据来源进行验证。
3. **诊断证据质量。** 已有假设驱动观察、源码引用和 systemd 证据，应优先评估它们是否减少补充询问、是否支持可验证的诊断结论。用脱敏真实案例补覆盖，再决定新增观察能力。
4. **任务成功统计。** 按 [#226](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/226) 区分工具调用成功、Runtime 最终 Outcome 和独立验收。触发改进 #233 已由用户确认完成，不再列为待开发项。

目前的失败记录不足以支持替换 Runtime Core、增加通用多 Agent 调度器或重做凭据存储。下一项能力开发应由可复现的任务缺口驱动。
