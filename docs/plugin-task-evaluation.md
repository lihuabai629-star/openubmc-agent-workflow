# 插件任务评估

候选包与公开版本分别使用独立的 Evaluation Lab subject。版本号相同也不能合并结果：源码提交、归档 SHA-256、内容清单、Runtime、实际加载的 Skills 与 MCP 都参与身份校验。历史 subject 不随本机安装状态变化。

从固定归档生成候选输入：

```bash
python scripts/plugin_task_evaluation.py prepare \
  --archive /absolute/path/candidate.tar.gz \
  --sha256 ARCHIVE_SHA256 \
  --output /absolute/path/candidate-evaluation
```

此命令只验证归档并导出 subject、开发数据集和未验证状态。在干净的 Evaluation Lab checkout 中使用 `python -m evaluation prepare-plugin --help` 所列参数加载导出的 subject 和同一归档，创建隔离安装。使用该安装产生的 Runtime record 初始化实验，再经现有 Codex harness 执行；两组分别绑定自己的 subject 和 Runtime，固定相同模型、客户端、任务、目标快照、环境和预算。运行环境不可混用另一 WSL 的配置目录。

`evaluation/plugin-tasks` 包含 Skill 正反向触发、缺凭据恢复、全局默认复用、IP 覆盖、BMC/OS 隔离、配置更新、Conan remote 复用和 KB 恢复场景。目标与私有配置由独立执行环境提供，场景文件不保存秘密。配置更新和故障恢复场景需要执行端安排对应事件；没有该条件时保留缺口。仅结束 Agent 进程不构成任务成功。

先用 Lab 生成并验证两份完整 Evaluation Bundle。业务判据由独立任务复核记录补充，记录格式为：

```json
{
  "schema": "openubmc.plugin-task-review.v1",
  "bundle_digest": "sha256:...",
  "reviewer": "independent reviewer identity",
  "method": "independent-task-review",
  "samples": [{
    "episode_id": "actual episode identity",
    "source_digest": "sha256:...",
    "source_path": "raw/actual-episode/harness-evidence.json",
    "predicates": {"default_selected": true},
    "metrics": {"extra_tool_calls": 0, "human_interventions": 0, "recovery_attempts": 0},
    "evidence_refs": [{"path": "raw/actual-episode/agent-events.jsonl", "sha256": "..."}]
  }],
  "digest": "sha256:..."
}
```

`digest` 使用 Lab 的 `digest_document` 规则。每条判定必须引用对应 Bundle 内的实际证据及文件摘要；任务评估工具校验摘要和 episode 原始来源绑定。它校验判定的归属与完整性，判定是否符合事实仍由独立复核负责。required predicate 的 `true` 表示行为成立；forbidden predicate 的 `true` 表示已确认该行为没有发生。未审查不能填 `true`。失败可填写 `failure_layer` 为 `environment`、`model`、`tools` 或 `runtime`；证据不足使用 `unclassified`。

`wall_seconds` 是完整任务耗时，`cost_usd` 是实际账单口径成本；有 token 数据但没有价格依据时成本仍未测量。`extra_tool_calls` 是独立复核识别的冗余调用数；`human_interventions` 计任务中的人工介入，`recovery_attempts` 计恢复尝试。观察到零可填零，未观察则省略。复核只能补充缺失指标，不能改写 harness 已记录的测量值。

```bash
python scripts/plugin_task_evaluation.py report \
  --lab /absolute/path/clean-evaluation-lab \
  --baseline-bundle /absolute/path/baseline-bundle \
  --baseline-subject /absolute/path/baseline-subject.json \
  --baseline-review /absolute/path/baseline-review.json \
  --candidate-bundle /absolute/path/candidate-bundle \
  --candidate-subject /absolute/path/candidate-subject.json \
  --candidate-review /absolute/path/candidate-review.json \
  --output /absolute/path/task-comparison.json
```

报告复用 Lab 的完整证据重算校验，记录评估器提交及两组身份，以 case 和 repetition 配对。完整任务成功率使用全部计划样本作分母；耗时、成本及介入指标分别报告全部任务与成功任务，配对差值仅使用两组都成功的同一任务。缺样本、缺业务判据或缺指标返回 `unverified`，退出码为 2；完整证据下任务失败返回 `failed`。报告不推断未覆盖的 BMC、Conan、KB、Windows 或跨 WSL 资格。

两组必须对应不同的 subject 和归档 SHA-256；同一归档改换描述名或路径仍不能作为候选与基线的比较。实际目标输入以每个样本的 `target_input_digest` 绑定；任一组缺少有效摘要时，该样本保留 `unverified` 缺口，不能因双方都缺失而视为已配对。没有目标的任务也应记录空对象的摘要。Lab 可验证历史兼容格式，并不代表其中缺失的目标身份足以支持新的配对资格。
