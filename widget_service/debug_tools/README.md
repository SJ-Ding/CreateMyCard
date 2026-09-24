# AI Widget Debug Tools

`debug_tools` 是 `main-agent-debug` 分支的本地浏览器测试工作台，包含三个业务模块和一个统一 React 壳：

- `end_to_end_debug/`：端到端 Main Agent 调试服务、事件轨迹和产物检查器。
- `interface_debug/`：三个工具 WebSocket 接口的手工调试、流帧解析、跨接口参数构建。
- `card_renderer/`：A2UI、Compact DSL 和 Design Compact 产物预览。
- `platform/`：React/Vite 组合应用，负责路由、共享事件时间线和模块间产物传递。

## 本地启动

先在一个终端启动现有工具服务（默认 `127.0.0.1:8855`）：

```powershell
cd widget_service
.venv\Scripts\python.exe cloud\start_websocket_server.py
```

再启动调试平台后端（默认 `127.0.0.1:8888`）：

```powershell
cd widget_service
.venv\Scripts\python.exe -m debug_tools.end_to_end_debug.backend.server
```

开发时可直接启动 Vite：

```powershell
cd widget_service\debug_tools
npm install
npm run dev
```

打开 <http://127.0.0.1:5173/debug/>。生产/静态预览先运行 `npm run build`，构建结果由端到端服务挂载到 `/debug/`。

## 调试入口

| 用途 | 地址 |
| --- | --- |
| 工作台 | `http://127.0.0.1:8888/debug/` |
| 健康检查 | `http://127.0.0.1:8888/debug/health` |
| 端到端 WebSocket | `ws://127.0.0.1:8888/debug/e2e/ws` |
| 工具 WebSocket 代理 | `ws://127.0.0.1:8888/debug/tools/{operation}` |

工具服务仍由外部进程独立启动；平台不会自动拉起或回收 8855 服务。BFF 只允许转发
`getWidgetCapabilityOverview`、`getDataCapabilitySchemas` 和
`generateWidgetCardCompactDsl` 三个操作，并保持原始 WebSocket 帧不变。

## 范围说明

来源 `websocket_debugger` 分支中的旧 `/api/v1/ws/agent/chat` 智能体调试协议没有并入本平台，避免与当前
`/debug/e2e/ws` 的 Main Agent 协议混用。`widget_service/docs/index.html` 保留为独立的渲染回退页和行为对照基线。
