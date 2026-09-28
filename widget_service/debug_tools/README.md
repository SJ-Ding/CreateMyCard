# AI Widget Debug Tools

`debug_tools` 是本地浏览器测试工作台，包含三个业务模块和一个统一 React 壳：

- `end_to_end_debug/`：端到端 Main Agent 调试服务和 Agent 事件轨迹。
- `interface_debug/`：浏览器直连三个工具 WebSocket 接口的手工调试、final 解析和跨接口参数构建。
- `card_renderer/`：A2UI、Compact DSL、Design Compact 产物预览和 Artifact 检查器。
- `platform/`：React/Vite 组合应用，负责路由、共享配置、左侧接口调用历史和模块间产物传递。

平台壳固定为 `100dvh`。`navigation-rail` 下半部的“接口调用历史”只记录三个微服务调用的 request、`final`/`final_error` 和状态；Agent 的普通事件仍只显示在端到端模块内部，不会把中间 WebSocket 帧混入共享历史。

## 本地启动

接口调试和卡片渲染可以完全在浏览器中运行，不需要启动本项目后端。先启动或准备可从浏览器访问的三个微服务 WebSocket（默认 base 为 `ws://127.0.0.1:8855/api/v1/ws/tools`）：

```powershell
cd widget_service
.venv\Scripts\python.exe cloud\start_websocket_server.py
```

如果当前环境可以使用 Node.js，可以启动前端开发服务器：

```powershell
cd widget_service\debug_tools
npm install
npm run dev
```

打开 <http://127.0.0.1:5173/debug/>，进入“连接配置”填写 `toolWsBaseUrl` 和其它固定参数。Vite 的 `/debug` 代理只服务同源 Agent/健康检查路径，不代理微服务 WebSocket；接口模块会按 `toolWsBaseUrl` 直接连接 8855。没有 Agent 后端时，“接口调试”和“卡片渲染”仍可用；“端到端调试”会显示 Agent 未连接。

### Python-only 启动（无需 npm）

仓库已经包含可由后端静态托管的前端构建产物。在只有 Python 的环境中，从项目根目录直接运行：

```powershell
python -m debug_tools --host 127.0.0.1 --port 8888
```

然后打开 <http://127.0.0.1:8888/debug/>。该入口同时提供工作台静态文件、健康检查和 Agent WebSocket；不会启动或代理 8855 微服务。若需要重新编译前端，才需要 Node.js，日常浏览器调试不依赖 `npm run dev`。

若需要端到端 Main Agent，再另开终端启动调试后端（默认 `127.0.0.1:8888`），或在连接配置中填写自定义 Agent URL：

```powershell
cd widget_service
.venv\Scripts\python.exe -m debug_tools.end_to_end_debug.backend.server
```

生产/静态预览先运行 `npm run build`，构建结果由任意静态服务器挂载到 `/debug/`；只有端到端链路需要可访问的 Agent WebSocket。

## 调试入口

| 用途 | 地址 |
| --- | --- |
| 工作台（Vite 开发） | `http://127.0.0.1:5173/debug/` |
| 工作台（后端静态托管） | `http://127.0.0.1:8888/debug/` |
| 健康检查 | `http://127.0.0.1:8888/debug/health` |
| 默认 Main Agent WebSocket | `ws://127.0.0.1:8888/debug/e2e/ws`（`/debug/agent/ws` 为兼容别名） |
| 默认微服务 WebSocket base | `ws://127.0.0.1:8855/api/v1/ws/tools` |
| 单个微服务接口 | `{toolWsBaseUrl}/{operation}` |

平台不会自动拉起或回收微服务。三个允许的 operation 为
`getWidgetCapabilityOverview`、`getDataCapabilitySchemas` 和
`generateWidgetCardCompactDsl`。浏览器直接连接 `toolWsBaseUrl`，接收帧直到
`final`/`final_error`，再关闭该连接；`/debug/tools/{operation}` 仅返回
`BROWSER_DIRECT_REQUIRED` 迁移提示，不是 BFF 代理。

### Agent dotted 协议

端到端页面使用 `protocolVersion: "1.0"`：

1. `conversation.open` → `conversation.ready`（获得 `conversationId`）。
2. `turn.start`（非空 `text`）→ `turn.status`、`assistant.message`、`tool.call`、`artifact_preview`、`turn.completed` 或 `error`；`waiting_tool` 和 `tool.trace` 仅作协议内部状态，不进入运行轨迹。
3. 浏览器收到 `tool.call` 后执行对应微服务，并回传带匹配 `turnId`、`callId`（建议同时带 `conversationId`）的 `tool.result`；后端返回 `tool.result.accepted`/`duplicate`/`rejected` 后继续 Agent。
4. `turn.cancel` 和 `conversation.reset` 分别取消当前回合和重建会话。旧 `message`/`configure`/`reset` 仅为兼容独立客户端保留。

工具桥和浏览器调用的默认超时均为 180 秒。断线、连接错误、非 JSON、服务端关闭和超时会结束当前调用并写入失败状态；中间 `start`/`partial`/`command` 帧不进入左栏共享历史。后端只校验调用关联 ID、operation、final 终态标记、结果 JSON 可序列化性和 4 MiB 大小上限，不持有微服务连接。

## 调试模型配置

端到端 Main Agent 与微服务使用相同的 provider 名称、主备切换、失败重试、并发和超时配置语义。
配置优先级为 `cloud/config/default_config.yaml` < `end_to_end_debug/backend/debug_agent.yaml` 的
`model` 节 < `widget_service/.env`；进程环境变量高于三个文件。修改后需重启调试后端。

实际模型传输只由 `WIDGET_SERVICE_OPENAI_MASTER_CLIENT` 选择，支持 `deepseek_official_http`、
`deepseek_platform` 和 `llmclient`。配置 API key 或 URL 本身不会隐式改变 provider。官方 HTTP 使用
`WIDGET_SERVICE_DEEPSEEK_OFFICIAL_HTTP_*`，平台 WebSocket 使用
`WIDGET_SERVICE_DEEPSEEK_PLATFORM_*`。API key 只从服务端读取，不会下发到浏览器或写入事件日志。

## 范围说明

Main Agent 后端只负责 Skill 导入、模型调用和浏览器工具等待桥；API key 只从服务端读取，不下发浏览器。浏览器配置保存在
`localStorage` 的版本化键 `ai-widget-debug-config:v1`，其中 `agentWsUrl` 与 `toolWsBaseUrl` 分开设置。
后端配置中的 `upstream_base_url` 仅为兼容旧测试/配置保留，浏览器直连路径不会让后端代理微服务。

来源 `websocket_debugger` 分支中的旧 `/api/v1/ws/agent/chat` 协议没有并入本平台，避免与当前 dotted
`/debug/e2e/ws` 的 Main Agent 协议混用。`widget_service/docs/index.html` 保留为独立的渲染回退页和行为对照基线。
