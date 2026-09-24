# 接口调试（`@widget-debug/interface`）

这个子包把 `websocket_debugger` 分支原来的手工页面拆成可嵌入统一工作台的 React 组件，当前只覆盖正式的三个工具接口：

- `getWidgetCapabilityOverview`
- `getDataCapabilitySchemas`
- `generateWidgetCardCompactDsl`

入口组件是 `InterfaceDebugger`，稳定属性如下：

```tsx
<InterfaceDebugger
  transportBase="/debug/tools"
  onEvent={(event) => timeline.push(event)}
  onArtifact={(artifact) => renderer.open(artifact)}
/>
```

`transportBase` 可以是同源路径或完整 `ws://`/`wss://` 地址，组件会在其后追加接口名称。为兼容旧平台壳，还支持 `socketBasePath` 别名。组件不会启动或管理 8855 工具服务，也不会调用来源分支的 `/ws/agent/chat` 智能体协议。

## 功能

- 三个接口 Tab，各自保存请求表单状态；通用设备/会话信封按来源页面的字段构造。
- 一次调用保留 `start`、`partial`、`command`、`final`、`final_error` 原始帧，并通过 `onEvent` 向统一时间线报告收发事件。
- `final.streamContent` 支持 JSON、JSONL fenced block、Python repr 以及 `data={...}` 旧消息包装；解析过程不使用 `eval`。
- 历史请求可跨接口回看；解析结果以可展开树展示，字段可勾选并生成请求片段。
- 提供 `dataCapabilityIds`、`candidateDataBindings`、`candidateAssetIds`、`candidateEventCandidates` 和“选中内容”快速构建；构建结果可回填到业务参数 JSON。
- 解析到 artifact 后调用 `onArtifact`，供卡片渲染器显式接收；不会在浏览器中代理任意 `artifactUrl` 下载。

## 本地检查

```powershell
cd widget_service\debug_tools
npm install
npm --workspace @widget-debug/interface run typecheck
npm --workspace @widget-debug/interface run test
```

统一平台通过 `@widget-debug/interface` workspace 依赖导入该组件。
