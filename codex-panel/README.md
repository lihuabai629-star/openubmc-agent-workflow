# openUBMC Codex 面板

插件内置原生面板，通过 stdio 提供 `openubmc_panel` 全局入口和私有只读工具。
面板按需连接已安装插件，支持知识库问答、Run 回执、Runtime 状态与证据读取。
界面位置和入口展示由 Codex 宿主决定。

## 开发

```bash
cd codex-panel
npm ci
npm run build
npm run check
npm test
npm run check:native -- --plugin-root /absolute/path/to/installed/openubmc
```

`npm run build` 同时生成本地预览资源和插件内的独立 Node 面板包。
插件 `.mcp.json` 注册该包，无需另建 HTTP 服务。

## 本地预览

```bash
npm start -- --plugin-root /absolute/path/to/installed/openubmc --port 8765
```

访问 `http://127.0.0.1:8765/preview`。服务仅监听回环地址，调用需要页面令牌并检查来源。
打开页面不连接设备、不执行 Run。面板不重放失败操作，不写 Runtime 状态。
Windows 客户端通过指定的 WSL 发行版启动 Linux 插件，后端和账号都位于 WSL 内。

MCP Apps 资源与旧宿主的 `window.openai` 桥均保留，便于 Codex 宿主兼容。
