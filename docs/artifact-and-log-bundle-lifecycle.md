# Artifact 与 Log Bundle 生命周期

Runtime Core 通过 `ArtifactStore` 统一管理大对象内容和生命周期。Run、Turn 与 Domain
Result 只保存 `ArtifactRef`；实际字节、存储路径、红化状态和过期状态不进入 Agent
上下文。

## ArtifactStore Interface

`ArtifactStore` 的外部 Interface 保持为少量内容操作：

- `put`：复制并内容寻址一份 Runtime 管理的原始内容；
- `resolve`：校验 kind、target、Run、digest、size、保留状态与红化要求后返回本地路径；
- `redact`：从原始 Artifact 派生新的红化内容和 digest，不修改或重标记原字节；
- `release_run`：显式释放 `run-lifetime` 引用；
- `garbage_collect`：删除到期元数据，并仅在没有剩余引用时删除 Runtime 管理的内容；
- `find`：按 Effect、kind、target 与 Run 找回已有结果，用于稳定重放。

Artifact 元数据通过独立 Repository 持久化。SQLite Adapter 可以跨进程重启恢复；
内存 Adapter 用于行为测试。托管内容使用 `artifact://sha256/<digest>` 句柄，外部构建
产物仍可使用本地文件句柄，但首次解析时必须完成内容验证并登记元数据。

保留类型固定为：

| 类型 | 生命周期 |
| --- | --- |
| `temporary` | 到达配置 TTL 后可回收 |
| `run-lifetime` | Run 显式释放前保留 |
| `audit` | 不参与自动过期 |

## Log Bundle 四阶段

Log Analyzer 的 Runtime Module 使用同一个 ArtifactStore，并将原来的混合处理拆成：

| 阶段 | 输入 | 输出 | Effect 语义 |
| --- | --- | --- | --- |
| `collect` | target 与采集参数 | `openubmc-log-bundle` | 可能触发目标生成 dump；不按只读 Effect 自动重试 |
| `index` | bundle ArtifactRef | `openubmc-log-index` | 本地只读，生成确定性内容清单 |
| `query` | index ArtifactRef + 有界问题 | `openubmc-log-query` | 本地只读，结果按字节预算收缩并派生红化 Artifact |
| `export` | 红化 query ArtifactRef | `openubmc-log-report` | 本地只读，生成红化报告 Artifact |

Index 保存归档内相对路径、大小与 SHA-256。Query 重新读取 bundle 时先验证 index
清单，随后才执行有界日志选择与证据抽取。Export 只接受已登记为红化的 Query
Artifact，避免把原始日志误标成可分发报告。

四阶段是 Runtime 内部 operation/Domain Pack contract。默认 Agent Interface 仍只有
`observe` 与 `execute`；CLI 与 compatibility Adapter 可以组合阶段，但不能改变 Runtime
对 Artifact 身份和访问范围的判断。
