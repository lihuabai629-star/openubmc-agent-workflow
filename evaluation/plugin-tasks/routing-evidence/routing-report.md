# openUBMC 原生 Skill 路由对比

插件通过 12/12 个路由判据用例，loose Skills 基线通过 8/12 个，按各安装形态的意图所有者判据增加 4 个。其中 3 个可配对用例改善，1 个 KB 用例因安装能力不同不计入发现性增量，8 个用例保持通过，其中 2 个负向/含糊用例两组均未误触发。

## 执行与输入身份

- Codex CLI：`0.153.4`；模型：`gpt-5.6-sol`；effort：`xhigh`；provider：`cliproxy`。
- 原生入口：`originator=codex_exec`；安全策略：`approval_policy=never`，`sandbox=read-only`。每个 rollout 和多轮 resume 都从原生 session 记录反验。
- 评估器：提交 `e0d534250ce825b0d80106eec12138129339458a`，tree `686abc09bf5e8a54109fe8de8f8023fd9e3baf2a`；runner `sha256:4ed7eef0011db7878669f4e03d29aad3c440e91b14af77dfde9887f90636ecbc`，matrix `sha256:f6a93fabc04463a8865e47faa568fb045b19c04156498813777a99119cdfef4d`，review contract `sha256:88f288a09bfa2ecb8c30ab156840596c1e95354949cae9910d385df1a4715629`。三个输入均已与该提交中的精确字节核验。
- 外部环境：两臂均未提供目标或凭据；KB MCP 可用性属于安装形态这一处理变量。loose 臂为 `unavailable-in-loose-arm`，插件臂为 `configured-without-credentials`。
- 源码：`lihuabai629-star/openubmc-agent-workflow@3a898818b5ea5bd5f810ecd3c22de6617ae617da`，tree `eab8fda257f1d9802cf5fad82f10223e1f8820e1`。
- 插件：`openubmc 2.0.12`；archive `sha256:caa1a7605eab1e0dc6b58de3b0b96b9b89ec53dbb0f12f79258d0f87dba6d94d`；content `2ce74f4b0aeebd6fd1f1fc268c1611c92bf57179961ec44c380e6e074c31384d`；subject `sha256:caf3583a199687176b38b3d6d0a9ce19d3c6ae74fab1dc2918e0f4351847cbde`；Runtime `sha256:bd511c205cb754613795142d6cd91e1138802a33580c636a9e74e363a4d4d692`。原生 inventory 含 12 个插件 Skill 和 2 个 MCP。
- loose Skills：原生 inventory 含 30 个 openUBMC 条目；346 文件内容清单 `sha256:82305fb4230891357ab0beafa7450dc5c986a49709b1fde87de066dfcc61c923`；配置 `sha256:2f56fae78781bbe94cb1ec3abcafbe738c6bc9889a6d23742716766dd13af830`。其上游仓库为 dirty 状态，内容清单是权威身份。
- 目录覆盖：9 个普通空目录用例、2 个从普通目录引用外部源码的用例、1 个源码 cwd 用例。普通目录已验证为空且不属于 Git，源码目录已验证为上述干净提交。

## 实际读取与调用

| 用例 | 目录 | loose Skills 实际读取/调用 | 插件实际读取/调用 | 路由变化 |
|---|---|---|---|---|
| `diagnosis-zh-ordinary` | ordinary | `openubmc-debugging` | `openubmc-debug` | `passed` → `passed` |
| `diagnosis-en-ordinary` | ordinary | `openubmc-redfish-testing`, `openubmc-debugging` | `openubmc-debug` | `passed` → `passed` |
| `logs-en-ordinary` | ordinary | `openubmc-debugging` | `openubmc-log-analyzer` | `skill-not-loaded` → `passed` |
| `build-zh-explicit-source` | explicit-source | `openubmc-bingo-build` | `openubmc-build` | `passed` → `passed` |
| `build-en-source-cwd` | source | `openubmc-bingo-build` | `openubmc-build` | `passed` → `passed` |
| `upgrade-en-ordinary` | ordinary | `openubmc-redfish-testing` | `openubmc-upgrade` | `skill-not-loaded` → `passed` |
| `credentials-zh-ordinary` | ordinary | `openai-docs` | `openubmc-environment-setup` | `skill-not-loaded` → `passed` |
| `kb-en-ordinary` | ordinary | `openubmc-debugging` | `openubmc-debug`, `openubmc_kb_query` failed, `openubmc_kb_status` completed | `mcp-not-loaded` → `passed` |
| `component-zh-explicit-source` | explicit-source | `openubmc-mdb-interface-dev` | `openubmc-developer` | `passed` → `passed` |
| `negative-zh-ordinary` | ordinary | — | — | `passed` → `passed` |
| `ambiguous-zh-ordinary` | ordinary | — | — | `passed` → `passed` |
| `context-en-multi-turn` | ordinary | `openubmc-debugging` | `openubmc-debug` | `passed` → `passed` |

多轮用例逐 turn 核验：第一轮没有提前读取 openUBMC Skill，补充 openUBMC 上下文后第二轮才读取 Debug。Skill 读取只有在命令完成、退出码为零且输出含对应 `SKILL.md` frontmatter 名称时才成立。

## 耗时与 token

| 评测臂 | 总耗时 | 输入 token | 输出 token | 总 token |
|---|---:|---:|---:|---:|
| Loose Skills | 2010.24s | 1,782,690 | 38,474 | 1,821,164 |
| 插件 | 1134.87s | 1,234,252 | 27,644 | 1,261,896 |

单次样本合计差值为 -875.36s、-559,268 token。没有可信价格依据，USD 成本未测量；每个用例只有一次 rollout，这些数据不支持统计性能结论。

代表性构建用例：显式外部源码从 264.83s / 323,014 token 降至 194.86s / 227,249 token；源码 cwd 从 580.73s / 471,843 token 降至 203.05s / 263,554 token。插件诊断 Skill 仍会读取较多参考资料，存在进一步压缩输入 token 的空间。

## 错误归因

- loose 基线的 3 个用例归因为 `skill-not-loaded`：对应意图所有者在原生 inventory 中 disabled。逐臂 review contract 将 `openubmc-debugging`、`openubmc-bingo-build` 和 `openubmc-mdb-interface-dev` 视为 loose 安装的有效旧入口，未把这些实际触发误写成失败。
- loose KB 用例归因为 `mcp-not-loaded`：该安装形态没有 `openubmc-kb` MCP，不能据此判断模型是否会触发一个并不存在的工具。插件 KB 已读取相关 Debug Skill 并实际调用 query/status；query 返回 `KB_CREDENTIALS_MISSING`，属于 `environment-or-auth`，不属于路由失败。
- 插件凭据用例的 Skill 路由通过，但 `pluginctl.py doctor` 返回退出码 2；结构化输出归因为 `environment-or-auth`（`CREDENTIALS_MISSING`, `READ_ONLY_FILESYSTEM`）。模型如实报告凭据缺失和只读锁限制，路由成功没有覆盖该环境失败。
- loose 的两个 rollout 出现可恢复的模型流超时并继续到 `turn.completed`，记录为 `model-transport-recovered`，未误判为终态执行失败。
- 当前矩阵没有出现预期 Skill 已启用并读取、随后因内部实现能力缺失而失败的样本。已有 `skill-positive` 的 systemd/journal 采集失败属于 #228 的 Runtime 能力缺口，不计入 #233 路由结果。
- 未提供目标、设备凭据或真实包；没有联系、重启、刷写或修改 BMC。设备任务未被宣称完成。

## 最小改进

1. 保留已合并的插件中英文描述；本轮 3 个可配对正向用例改善，其余有效旧入口保持通过，没有证据支持继续扩大默认 prompt。
2. loose 安装先校正目录 `enabled=true` 与直接 `SKILL.md enabled=false` 的重复配置，再重新探测 canonical Skill inventory。本轮证明了 canonical Skill 被禁用，没有单独证明 Codex 的路径解析因果。
3. 后续单独精简 Debug/Build 的参考资料读取；该问题影响 token 和延迟，不改变本轮路由结论。
4. systemd/journal 当前状态采集继续由 #228 修复，不并入 Skill 发现性改动。

## 证据边界

原始 JSONL、stderr 和完整 session 仅保留在本地；提交了可重放的脱敏 native event、stderr 与 rollout 身份投影，共 51 个 loose 文件和 51 个 plugin 文件，并保留原始文件 SHA-256 绑定。loose evidence：`sha256:6c0abcc1d6c19de254e2286a1c10d1516abe566c22cd8e511388d5ba47352b23`；plugin evidence：`sha256:5957805ae6e4df2a7bc9113e8b1da09ed4ce596a09e33032c1ec79337be1701a`。提交记录已扫描当前凭据值、私钥、GitHub token、JWT 和 RFC1918 地址。

本记录只验证 Linux WSL2 中的原生 Codex CLI 路由；未验证原生 Windows、真实 BMC、Conan 发布或升级完成度。
这是 routing-only 原生 Codex 记录，不替代完整 Evaluation Lab Bundle 和 independent task review。
