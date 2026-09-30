# AI Widget Debug Tools

`debug_tools` 是本地浏览器测试工作台，包含四个业务模块和一个统一 React 壳：

- `end_to_end_debug/`：端到端 Main Agent 调试服务和 Agent 事件轨迹。
- `interface_debug/`：浏览器直连三个工具 WebSocket 接口的手工调试、final 解析和跨接口参数构建。
- `card_renderer/`：A2UI、Compact DSL、Design Compact 产物预览和 Artifact 检查器。
- `batch_testing/`：读取固定测试数据集、并发调用 Compact DSL 接口、保存结果并解析本地 Trace。
- `platform/`：React/Vite 组合应用，负责路由、共享配置、左侧接口调用历史和模块间产物传递。

平台壳固定为 `100dvh`。`navigation-rail` 下半部的“接口调用历史”只记录三个微服务调用的 request、`final`/`final_error` 和状态；Agent 的普通事件仍只显示在端到端模块内部，不会把中间 WebSocket 帧混入共享历史。

## 本地启动

接口调试和卡片渲染可以完全在浏览器中运行；批量测试需要由统一 Python 入口提供本地文件 API。先启动或准备可从浏览器和调试平台后端访问的三个微服务 WebSocket（默认 base 为 `ws://127.0.0.1:8855/api/v1/ws/tools`）：

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

### 统一 Python 启动入口

前端构建产物统一位于 `debug_tools/dist/`。构建完成后，从 `widget_service` 目录选择启动模式。

只启动静态前端，不加载 Main Agent 配置和模型客户端：

```powershell
uv run debug_tools --mode frontend --host 127.0.0.1 --port 8888
```

同时启动静态前端和 Main Agent 后端：

```powershell
uv run debug_tools --mode full --host 127.0.0.1 --port 8888
```

没有使用 `uv` 时，可将上述命令替换为
`python -m debug_tools --mode frontend|full --host 127.0.0.1 --port 8888`。

两种模式均打开 <http://127.0.0.1:8888/debug/>，且都不会启动或回收 8855 微服务。
两种模式均提供批量测试本地 API；单次接口调试仍由浏览器直连微服务，批跑后端仅为读取数据集、
写入结果和关联 Trace 而连接配置的微服务地址。`frontend` 模式下接口调试、批量测试和卡片渲染可用，
端到端页会显示 Agent 未连接；`full` 模式额外提供
`/debug/health`、`/debug/skills`、`/debug/artifact` 和 Main Agent WebSocket。旧的嵌套
`start_server.py` 与直接运行 `backend.server` 的入口已移除。

## 调试入口

| 用途 | 地址 |
| --- | --- |
| 工作台（Vite 开发） | `http://127.0.0.1:5173/debug/` |
| 工作台（统一 Python 入口） | `http://127.0.0.1:8888/debug/` |
| 健康检查（仅 `full`） | `http://127.0.0.1:8888/debug/health` |
| Main Agent WebSocket（仅 `full`） | `ws://127.0.0.1:8888/debug/e2e/ws`（`/debug/agent/ws` 为兼容别名） |
| 默认微服务 WebSocket base | `ws://127.0.0.1:8855/api/v1/ws/tools` |
| 单个微服务接口 | `{toolWsBaseUrl}/{operation}` |
| 批量测试页面 | `http://127.0.0.1:8888/debug/batch` |
| 批跑数据集 | `debug_tools/test_datas/request_dataset/*.json` |
| 批跑输出 | `debug_tools/batch_output/<runId>/` |

平台不会自动拉起或回收微服务。三个允许的 operation 为
`getWidgetCapabilityOverview`、`getDataCapabilitySchemas` 和
`generateWidgetCardCompactDsl`。浏览器直接连接 `toolWsBaseUrl`，接收帧直到
`final`/`final_error`，再关闭该连接；`/debug/tools/{operation}` 仅返回
`BROWSER_DIRECT_REQUIRED` 迁移提示，不是 BFF 代理。

### 批量测试

“批量测试”页面固定从 `test_datas/request_dataset` 读取完整工具请求包络，可勾选样本并设置并发数
（1～16，默认 4）和额外失败重试次数（0～5，默认 1）。每次尝试使用
`batch-<8位运行标识>-<5位样本序号>-<3位重试序号>` UID，并覆盖请求中的
`content.uid` 和 `userAuth.user.userId`，不修改源数据文件。

批跑结果按轮次保存 `manifest.json`、`summary.json`、`summary.md` 和每个样本的请求、响应、
artifact blocks、`genui.jsonl`、Trace 与最终结果。页面可恢复已完成轮次，并在详情中预览成功 DSL。
微服务需要预先启动；如需 Trace，还要启用 `enable_generation_trace_recording`，并确保
`generation_trace_root` 指向调试平台可读取的本地目录。Trace 缺失或单行解析失败只显示告警，
不会把已经成功生成的样本改判为失败。

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

卡片渲染保留现有 artifact 选择、编辑器和批跑复用流程，
底层统一使用从根目录 `render` 迁入的 Parser、UIGraph、组件注册表与 `renderTree`。根目录
`render` 保留为上游行为对照，不作为第二个调试平台入口。

本地素材由 `/resources/{path}` 和 `/background_assets/{path}` 提供，只允许读取固定资源根内的文件。
外部图片不经过服务端代理；卡片动作仅在浏览器中展示解析参数，不执行跳转。
