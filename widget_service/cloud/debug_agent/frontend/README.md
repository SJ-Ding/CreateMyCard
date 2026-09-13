# Debug Agent Workbench

这是 Debug Agent 的 React + TypeScript + Vite 前端源码。构建结果会写入上级的 `static/` 目录，由 FastAPI 直接提供。

```powershell
npm install
npm run typecheck
npm run build
```

开发预览使用 `npm run dev`；正式页面仍由 `cloud/start_debug_agent_server.py` 提供。
