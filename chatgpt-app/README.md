# openUBMC Panel

从 Codex 原生侧栏打开工作流回执、证据和知识库，
也可在浏览器中本地运行，或通过 MCP Apps 接口连接 ChatGPT。
工作流仍由本机插件的 Runtime 执行，设备凭据保留在连接器主机。

## 本地运行

需要 Node.js 20+，以及已完成环境准备的 openUBMC 插件。
`--plugin-root` 指向包含 `.codex-plugin/plugin.json` 和 `scripts/openubmc-mcp-bootstrap.js`
的已安装插件目录，不是本源码仓库的根目录。可从 `codex plugin list` 查看插件安装信息。

```bash
cd chatgpt-app
npm ci
npm run build
npm start -- --plugin-root /absolute/path/to/installed/openubmc --preview
```

本地面板地址为 `http://127.0.0.1:8765/preview`。可直接提交知识库问题、
读取已有 Run 和检查 Runtime 状态，无需连接 ChatGPT。打开页面不会自动查询知识库或连接设备。
继续工作流时，在 Codex 对话中发送 Run ID；本地面板仅转发列入白名单的只读工具。
本地接口仅接受回环地址及页面生成的请求令牌，不会替代 `/mcp` 的 OAuth 认证。
在 ChatGPT 中使用时，面板通过宿主调用 MCP 工具。

`OPENUBMC_PLUGIN_ROOT` 也可配置插件目录。启动后插件根目录及其完整性保持不变；
Runtime/KB 的依赖和设备凭据继续由现有插件管理。

## Codex 原生入口

需要支持 MCP 全局应用入口的 Codex 桌面版。使用与 Codex 相同主机上的 CLI 注册：

```bash
codex mcp add openubmc-panel -- node /absolute/path/to/chatgpt-app/src/native.js \
  --plugin-root /absolute/path/to/installed/openubmc
```

在 Codex 的“设置 → 插件 → MCP”中重新加载本机服务，然后在左侧“探索”中
将 **openUBMC** 置顶到侧栏。按钮直接打开原生应用页面，可独立于聊天任务使用。
客户端通过工具的 `openai/ui.entrypoints: [{"type":"global"}]` 发现入口，
通过 `ui.resourceUri` 读取自包含面板。此路径使用 stdio，无需域名、HTTP 服务或 OAuth。

Windows 桌面版需要在 Windows 的 Codex 配置中注册。若面板和已准备的插件位于 WSL，
可由 Windows CLI 启动对应发行版，例如在 PowerShell 中运行（替换实际路径）：

```powershell
codex mcp add openubmc-panel -- C:\Windows\System32\wsl.exe --distribution 0630 --exec `
  /usr/local/bin/node /absolute/path/to/chatgpt-app/src/native.js `
  --plugin-root /absolute/path/to/installed/openubmc
```

仅在 WSL 的远程 Codex 配置中注册不会提供 Windows 本机的全局入口。
面板连接的 Run 历史和知识库配置属于 `--plugin-root` 对应的后端主机。

原生适配器只向模型提供打开面板工具，知识库和 Operator 读取工具仅供面板调用；
原插件继续拥有 Agent 的 Runtime 和知识库工具注册。打开面板不会启动设备操作。
入口发现、打开页面和读取界面资源不启动后端；提交查询或读取回执时才连接所需服务。
面板通过私有工具 `openubmc_panel_read` 调用允许的读取操作，并在调用前检查后端的实际工具目录。
卸载入口使用 `codex mcp remove openubmc-panel`，不影响已安装的 openUBMC 插件。

若按钮出现但页面超时，先检查本机插件的启动健康状态。Codex 首次打开 MCP 应用时
会准备内部宿主会话；本机已有服务的启动延迟也可能影响页面加载。
本版本客户端的内部会话请求等待上限为 30 秒；提高面板的 `startup_timeout_sec`
不会改变这一上限。可先用下述命令检查面板自身的启动、界面资源和知识库读取链路。

```bash
npm run check:native -- --plugin-root /absolute/path/to/installed/openubmc \
  --query "openUBMC 的 MDB 是什么？"
```

检查通过 stdio 调用面板。省略 `--query` 时只检查入口与界面资源，
不连接知识库。它不代表宿主桌面已经完成页面渲染。

## 连接 ChatGPT

没有域名时，可使用 Cloudflare Quick Tunnel 临时生成 HTTPS 地址。安装 `cloudflared`
后，在独立终端运行并保持进程开启：

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

将它返回的 `https://<随机名称>.trycloudflare.com` 用作下面的公开地址，
并在重启连接器时移除 `--preview`。Quick Tunnel 重新启动后地址通常会改变；
此时需要更新 `--public-origin` 并重新连接 ChatGPT。固定地址也可以使用下面的反向代理方式。

1. 为此机器提供 ChatGPT 服务可访问的 HTTPS 地址，由反向代理转发到本机
   `127.0.0.1:8765`。保留 `Authorization`、`Mcp-Session-Id`、`MCP-Protocol-Version`
   和流式响应，代理超时至少 900 秒。不要公开 `/preview`。
2. 使用实际域名重新启动连接器：

   ```bash
   npm start -- --plugin-root /absolute/path/to/installed/openubmc \
     --public-origin https://your-openubmc.example
   ```

3. 如果你的 ChatGPT 账号支持自定义应用开发，在应用设置中启用开发者模式，
   创建应用，MCP URL 填写 `https://your-openubmc.example/mcp`，认证选择 OAuth。
   连接器提供 OAuth 发现、动态客户端注册和 PKCE。
4. 在授权网页输入连接器主机上的登录密钥。首次启动自动创建私有密钥文件，
   启动日志只显示文件路径。默认路径为
   `~/.local/share/openubmc/chatgpt-app/chatgpt-login-key`；设置 `XDG_DATA_HOME` 时使用对应数据目录。
   在本机查看密钥并填入授权网页，不要发送到聊天。
5. 在对话中选择此应用并说“打开 openUBMC 面板”。
   调用 `openubmc_panel` 会展示面板；后续 `observe`、`execute` 和知识库查询结果使用同一界面。

授权页授予对这台连接器主机的单用户访问。不要将一个实例共享给互不信任的用户。
访问令牌有效期为一小时，刷新令牌有效期为一天并轮换；重启连接器后需重新连接应用。
登录连续失败五次后暂停十五分钟。设备账号的配置和更新仍在原插件的私有配置页完成。

账号是否提供开发者模式、OAuth 回调和界面展示，需在你的 ChatGPT 账号上确认。
此连接器不调用 OpenAI 模型 API，也不需要 OpenAI API Key。

## 面板操作

- **保持显示**：请求宿主的 `pip` 模式。
- **展开**：请求 `fullscreen` 模式。
- **查看**：通过私有只读工具读取输入的 Run ID。
- **检查连接**：读取 Runtime 状态，不连接设备。
- **在对话中继续**：将当前 Run ID 发回对话，由模型按 Runtime 下一步继续处理。
- **知识库**：提交技术问题，使用知识库的回答模式，并展示回答及来源。

宿主通过 `availableDisplayModes` 声明支持的模式时，面板隐藏不支持的按钮。
右侧、悬浮或全屏的位置由 ChatGPT 决定；连接器没有自行注册固定右侧栏的接口。
面板不使用 `observe` 轮询 Run，也不自行提交 Gate 或创建 Outcome。

## 接口与验证

`/mcp` 使用 Streamable HTTP，且要求 OAuth。标准 MCP Apps 客户端读取
`text/html;profile=mcp-app` 资源；旧式 ChatGPT 宿主通过 `openai/outputTemplate`
读取 `text/html+skybridge` 资源。两者使用同一自包含 HTML，均无需 CDN 或浏览器直连后端。

HTTP 模式的模型工具是 `openubmc_panel`、Runtime 的 `observe` / `execute` 及知识库工具。
`runtime_status`、`case_read`、`evidence_query`、`evidence_read` 从独立 Operator
连接获取，使用 `ui.visibility: ["app"]` 和旧式私有可见性标记，仅供面板读取。
其他 Operator 治理工具和本地环境修复工具不转发。

输入与结构化 Runtime 结果保持原样，面板元数据只追加到 `_meta`；同一 HTTP 会话固定
task identity，同一请求 ID 保持 operation identity。HTTP 会话结束会关闭其 stdio 子进程，
空闲会话一小时后回收。断线或超时不会自动重放操作；已有 Run 必须保留原 Run ID。

```bash
npm run build
npm run check
npm test
```

自动化测试不访问 BMC 或真实知识库。ChatGPT 中的显示模式、OAuth 回调和端到端行为
仍需通过可访问的 HTTPS 部署和实际账号验证。

官方接口参考：

- <https://developers.openai.com/apps-sdk/build/chatgpt-ui/>
- <https://developers.openai.com/apps-sdk/deploy/connect-chatgpt/>
- <https://developers.openai.com/apps-sdk/reference/>

依赖锁定在 MCP SDK `1.32.1` 和 MCP Apps SDK `1.7.5`。
