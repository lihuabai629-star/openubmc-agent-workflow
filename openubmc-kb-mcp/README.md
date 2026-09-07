# openUBMC KB MCP

让 Codex 直接通过 stdio 使用 openUBMC LightRAG 知识库，不依赖编辑器进程或本地 HTTP 服务。

## 工作流程

1. 读取本地用户名和密码。
2. 检查 OneID 是否要求验证码。
3. 获取公钥并以 RSA PKCS#1 v1.5 加密密码。
4. 登录用户中心并维护 `.openubmc.cn` 登录 Cookie。
5. 通过 OAuth2 授权码流程获取访问令牌。
6. 携带 `Authorization: Bearer <token>` 调用社区 HTTPS RAG 网关。

如果账号要求验证码，服务会拒绝知识库调用并返回错误，不会尝试绕过验证码。Token 不写入日志；访问令牌和刷新令牌保存在当前系统用户的私有缓存中，用于跨 Codex 重启复用登录状态。

默认缓存位置：

- Windows：`%LOCALAPPDATA%\openubmc-mcp\token-cache.json`
- Linux：`~/.cache/openubmc-mcp/token-cache.json`

访问令牌临近过期时，MCP 会优先使用 `refresh_token` 自动续期；服务端不支持续期、令牌被撤销或缓存不可用时，才回退到用户名和密码登录。OneID 的服务端失效、账号锁定和验证码策略仍然有效，客户端不能将服务端令牌改成真正永久有效。

## 仓库内运行

需要 Node.js 20 或更高版本：

```bash
cd openubmc-kb-mcp
npm ci
```

`openubmc-environment-setup` 默认把配置写入
`~/.config/openubmc/kb-mcp.json`（或 `$XDG_CONFIG_HOME/openubmc/kb-mcp.json`）。
配置文件只需包含 OneID 凭据，服务端地址和 OAuth 桌面客户端参数已有内置默认值：

```json
{
  "username": "用户名",
  "password": "你的密码",
  "clientSecret": "本地授权配置中的客户端密钥"
}
```

配置文件缺失或凭据为空时 MCP 仍可启动，`openubmc_kb_status` 会返回未配置状态。

## 测试

```bash
npm test
npm run check
```

## 接入

正常使用由 `openubmc-environment-setup` 自动安装依赖、生成 launcher，并为 Codex 和 Claude 注册 stdio MCP。

连接成功后会出现：

- `openubmc_kb_query：查询知识库`
- `openubmc_kb_status：检查知识库状态`
- `openubmc_kb_list：列出知识库文档`

首次调用工具时才检查登录状态。OAuth Token 临近失效时会自动续期；知识库返回 401 时会使当前访问令牌失效，并自动续期或登录后重试一次。403 表示权限不足，直接返回错误。

错误回执的 `code`、`retryable` 和 `recovery` 分别表示原因、是否适合重试与下一步动作。文本回执也包含恢复提示；原始上游认证错误内容不会进入工具回执。

| 错误码 | 下一步 |
| --- | --- |
| `KB_CREDENTIALS_MISSING` | 在本地私有配置中填写凭据 |
| `KB_INTERACTION_REQUIRED` | 在本地完成交互认证后再调用 |
| `KB_AUTHENTICATION_FAILED` | 检查本地账号与认证配置 |
| `KB_PERMISSION_DENIED` | 检查账号是否有对应权限 |
| `KB_RATE_LIMITED` | 等待后再重试只读请求 |
| `KB_SERVICE_UNAVAILABLE` | 服务恢复后重试 |
| `KB_NETWORK_ERROR` | 检查网络后重试 |
| `KB_TOOL_FAILED` | 检查本地诊断信息，不自动重复调用 |

三个工具都支持紧凑 JSON 或 Markdown 文本。查询上下文、流水线历史、文档摘要和错误信息都有固定上限；文档列表每页 10–100 条，并返回服务端分页信息。MCP 仅用于候选定位，源码和目标环境证据仍是最终依据。
