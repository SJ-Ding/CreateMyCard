# WebSocket API Debugger - 设计文档与使用说明

## 1. 项目概述

### 1.1 项目定位

WebSocket API Debugger 是一个面向 GenUI Widget 卡片生成微服务的**前端调试工具**，用于在浏览器中可视化地调试三个核心 WebSocket 接口。该工具解决了三个接口之间**串行调用、数据传递**的痛点，提供了从接口返回结果中选取字段并自动组装为下一个接口入参的能力。

### 1.2 技术背景

GenUI Widget 卡片生成服务采用 WebSocket 协议通信，三个接口构成一条完整的卡片生成流水线：

```
getWidgetCapabilityOverview  →  getDataCapabilitySchemas  →  generateWidgetCardCompactDsl
      (获取能力概览)                  (获取数据Schema)              (生成卡片DSL)
```

每个接口的输出经筛选和组装后，作为下一个接口的输入参数。手动构造这些请求参数繁琐且容易出错，本工具通过可视化选择和智能转换大幅简化了这一流程。

### 1.3 技术栈

- **纯前端单文件实现**：单个 `index.html` 文件，内嵌 CSS 和 JavaScript，零依赖、零构建
- **使用方式**：直接在浏览器中打开 `index.html` 即可使用（`file://` 协议）
- **通信协议**：原生 WebSocket API

---

## 2. 后端微服务架构

### 2.1 服务入口

| 项目 | 说明 |
|------|------|
| 入口文件 | `cloud/start_websocket_server.py` |
| 框架 | FastAPI + Uvicorn ASGI |
| 默认监听 | `127.0.0.1:8855` |
| 路由前缀 | `/api/v1`（定义于 `api/routes.py:45`） |

### 2.2 请求信封格式（ToolRequestEnvelope）

所有三个接口共用统一的请求信封结构：

```json
{
  "content": { /* 接口特有的业务参数 */ },
  "deviceInfo": {
    "countryCode": "CN",
    "deviceFormation": "HDSpeaker",
    "deviceType": 0,
    "locale": "zh-CN",
    "phoneType": "CLS-AL30",
    "prdVer": "11.7.5.205",
    "sysVer": "EmotionUI_9.0.0",
    "romVersion": "CLS-AL30 6.0.0.328",
    "time": "20260707115342975"
  },
  "session": {
    "sessionId": "7676c2c8-a6d3-413c-8074-c62ed30db8de",
    "interactionId": "1",
    "isNew": false
  },
  "userAuth": {
    "user": { "userId": "test-user-001" }
  },
  "utterance": {
    "original": "",
    "type": "text"
  },
  "version": "1.0",
  "bundleName": "com.omega_w_0823.hmservice"
}
```

### 2.3 响应流式帧格式（WidgetPluginStreamResponse）

服务端通过 WebSocket 推送流式帧，包含以下类型：

| 帧类型 (streamType) | 说明 | 适用接口 |
|---------------------|------|----------|
| `start` | 请求开始确认 | 全部 |
| `partial` | 心跳帧（每6秒） | generateWidgetCardCompactDsl |
| `final` | 最终结果帧 | 全部 |
| `final_error` | 错误帧 | 全部 |

`final` 帧的 `streamContent` 字段包含 Python repr 格式的字符串，工具内置了解析器将其转换为 JSON。

---

## 3. 三个接口详细规格

### 3.1 getWidgetCapabilityOverview

**功能：** 获取当前设备和应用可用的数据能力、事件能力和素材资源的完整概览。

| 项目 | 说明 |
|------|------|
| WebSocket Path | `/api/v1/ws/tools/getWidgetCapabilityOverview` |
| 请求模型 | `CapabilityOverviewRequest` |
| 处理模式 | 同步查询（线程池执行） |

#### Content 参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `odid` | string | 否 | 开放设备ID |
| `bundleName` | string | 否 | 宿主应用包名 |

#### 返回数据结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `dataCapabilities` | array | 数据能力列表，每项含 `id` 和 `description` |
| `eventCapabilities` | array | 事件能力列表，每项含 `id`、`description`、`actionTemplate`、`dynamicArguments` |
| `assetCandidates` | array | 素材资源列表（70+项），每项含 `id` 和 `description` |
| `unavailableCapabilities` | array | 不可用能力列表 |

**数据能力示例：** ViewWeather、GetCalendarEvents、GetCountdownDays、GetAppUsageDuration、GetEarphoneInfo、GetPhoneBatteryInfo、GetHealthAndSportSummary

**事件能力类型：**
- `clickToApi` — 调用系统API（如打电话、清理内存）
- `clickToDeeplink` — 深度链接跳转（如打开设置页、天气页）
- `clickToIntent` — Intent跳转（如查看日程、导航）

---

### 3.2 getDataCapabilitySchemas

**功能：** 根据指定的数据能力ID列表，获取每个能力的详细输入/输出Schema定义。

| 项目 | 说明 |
|------|------|
| WebSocket Path | `/api/v1/ws/tools/getDataCapabilitySchemas` |
| 请求模型 | `DataCapabilitySchemasRequest` |
| 处理模式 | 同步查询（线程池执行） |

#### Content 参数

| 字段 | 类型 | 必填 | 校验规则 | 说明 |
|------|------|------|----------|------|
| `odid` | string | 否 | — | 开放设备ID |
| `bundleName` | string | 否 | — | 宿主应用包名 |
| `dataCapabilityIds` | list[string] | **是** | min_length=1 | 需要查询Schema的数据能力ID列表 |

#### 返回数据结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `dataCapabilities` | array | 每个能力的完整Schema定义 |
| `missingCapabilityIds` | array | 未找到的能力ID列表 |

**每个 dataCapability 的结构：**

```json
{
  "id": "ViewWeather",
  "type": "data",
  "description": "查询指定地区天气与预报",
  "inputSchema": {
    "type": "object",
    "properties": {
      "districtName": { "type": "string", "description": "区县名" },
      "prefectureName": { "type": "string", "description": "城市名" },
      "forecastDays": { "type": "integer", "description": "预报天数1-5" }
    },
    "required": ["districtName"]
  },
  "outputSchema": {
    "type": "object",
    "properties": {
      "location": { "properties": { "cityCode": {}, "districtName": {}, "prefectureName": {} } },
      "current": { "properties": { "temperatureC": {}, "temperatureText": {}, "condition": {}, ... } },
      "daily": { "type": "array", "items": { "properties": { "date": {}, "condition": {}, ... } } },
      "updatedAt": { "type": "string" }
    }
  },
  "defaultWriteResultTo": "/data/weather",
  "dependencies": { "requiredPackages": [{ "packageName": "com.huawei.hmos.weather" }] }
}
```

---

### 3.3 generateWidgetCardCompactDsl

**功能：** 根据用户需求描述和候选的数据绑定、事件、素材，生成卡片 Compact DSL 并转换为标准 A2UI 格式。

| 项目 | 说明 |
|------|------|
| WebSocket Path | `/api/v1/ws/tools/generateWidgetCardCompactDsl` |
| 请求模型 | `GenerateWidgetCardRequest` |
| 处理模式 | 异步生成（含心跳，6秒间隔） |
| 特殊说明 | 支持创建模式和编辑模式 |

#### Content 参数

| 字段 | 类型 | 必填 | 条件 | 说明 |
|------|------|------|------|------|
| `userQuery` | string | **是** | 始终必填 | 用户需求描述 |
| `title` | string | **是** | 创建模式 | 卡片标题 |
| `description` | string | **是** | 创建模式 | 卡片描述 |
| `size` | "2x2"/"2x4" | 否 | — | 卡片尺寸，默认 "2x2" |
| `sourceArtifactUrl` | string | 否 | 编辑模式 | 已有制品URL，存在则触发编辑模式 |
| `candidateDataBindings` | array | 否 | — | 候选数据绑定列表 |
| `candidateEventCandidates` | array | 否 | — | 候选事件列表 |
| `candidateAssetIds` | list[string] | 否 | — | 候选素材ID列表 |
| `options.allowDegradation` | bool | 否 | — | 是否允许降级，默认 true |

#### CandidateDataBinding 结构

```json
{
  "capabilityId": "ViewWeather",
  "arguments": { "districtName": "上海", "forecastDays": 1 },
  "writeResultTo": "/data/weather",
  "candidateOutputFields": [
    "/location/districtName",
    "/current/temperatureText",
    "/current/condition",
    "/current/airQuality",
    "/updatedAt"
  ]
}
```

#### CandidateEventCandidate 结构

```json
{
  "capabilityId": "event.open.weather",
  "action": {
    "call": "clickToDeeplink",
    "args": {
      "intentName": "Weather_CityCode",
      "bundleName": "",
      "abilityName": "",
      "uri": "{{ 'hww://...' + ${/data/weather/location/cityCode} }}"
    }
  }
}
```

#### 返回数据结构

```json
{
  "apiVersion": "v1",
  "status": "success",
  "artifactUrl": "https://test.invalid/widget/artifact.md",
  "artifactDigest": "sha256:test-artifact",
  "suggestSize": "2x4",
  "message": "已为你生成可用的桌面卡片。",
  "effectiveCapabilities": {
    "data": ["ViewWeather"],
    "event": [{ "id": "event.open.weather", "call": "clickToDeeplink", ... }],
    "asset": ["asset.drop_1"]
  }
}
```

---

## 4. 接口串行关系与数据流

三个接口构成一条完整的卡片生成流水线，数据从上游接口流向下游：

```
┌─────────────────────────────┐
│  getWidgetCapabilityOverview │
│  返回：                       │
│  - dataCapabilities[].id     │
│  - eventCapabilities[]       │
│  - assetCandidates[].id      │
└──────────────┬──────────────┘
               │ dataCapabilityIds
               ▼
┌─────────────────────────────┐
│  getDataCapabilitySchemas    │
│  返回：                       │
│  - inputSchema → arguments   │
│  - outputSchema → outputFields│
│  - defaultWriteResultTo      │
└──────────────┬──────────────┘
               │ candidateDataBindings
               │ candidateEventCandidates
               │ candidateAssetIds
               ▼
┌─────────────────────────────┐
│  generateWidgetCardCompactDsl│
│  返回：                       │
│  - artifactUrl               │
│  - effectiveCapabilities     │
└─────────────────────────────┘
```

### 数据转换映射表

| 源接口 | 目标接口 | 转换逻辑 |
|--------|----------|----------|
| Overview | Schemas | `dataCapabilities[].id` → `dataCapabilityIds` |
| Overview | CompactDsl | `assetCandidates[].id` → `candidateAssetIds`；`eventCapabilities[]` → `candidateEventCandidates` |
| Schemas | CompactDsl | `dataCapabilities[]` → `candidateDataBindings`（含 `writeResultTo`、`candidateOutputFields` 从 outputSchema 生成） |
| CompactDsl | Schemas | `effectiveCapabilities.data` → `dataCapabilityIds` |

### 自动参数填充规则

从 Schema 构建 `candidateDataBindings` 时：
- `arguments`：必填字段使用默认占位值（string → `"请输入"`，integer → `0`，boolean → `false`），优先使用 `sampleValue`
- `candidateOutputFields`：从 `outputSchema` 提取所有叶子节点路径（如 `/location/districtName`、`/current/temperatureText`）
- `writeResultTo`：直接取自 `defaultWriteResultTo`

---

## 5. 前端工具功能详解

### 5.1 整体布局

```
┌─────────────────────────────────────────────────────────┐
│  WebSocket API Debugger              [Status: ● Ready]  │
│  Server URL: [ws://127.0.0.1:8855          ]            │
├─────────────────────────────────────────────────────────┤
│  [getWidgetCapabilityOverview] [getData...] [generate..] │ ← API Tabs
├─────────────────────────────────────────────────────────┤
│  当前接口历史: [#1 12:00:01] [#2 12:05:30]              │
│  其他接口历史: [⤴#3 12:03:15 Overview] [⤴#4 ...]        │ ← History Bar
│                                              [清空历史]  │
├──────────────────────────┬──────────────────────────────┤
│  Request Configuration   │  Response                    │
│                          │  [Stream] [Parsed] [Build]   │
│  Common Envelope         │                              │
│  Device Info             │  内容区域...                  │
│  Interface-Specific      │                              │
│                          │                              │
│  [Preview JSON] [Send]   │                              │
└──────────────────────────┴──────────────────────────────┘
```

### 5.2 API Tabs（接口切换）

- 三个接口以 Tab 按钮形式展示，替代传统的下拉框选择
- 切换接口时自动保存当前表单状态，返回时还原
- Tab 底部蓝色边框指示当前激活的接口

### 5.3 History Bar（历史记录）

历史记录分为两行展示：

- **当前接口历史**：显示当前选中接口的请求记录，点击直接加载该请求的响应到右侧面板
- **其他接口历史**：显示其他接口的请求记录，黄色背景带 `⤴` 符号标识，点击后：
  1. 在 Response 面板中显示该历史记录的完整响应
  2. 出现 Transfer Banner 提示用户可以选择字段并推送
  3. 用户通过 Parsed Result 选择字段 → Build JSON 构建 → Apply to Form 推送到当前接口表单
- **发送新请求**：发送前取消当前加载记录及其已选字段的引用；服务端完成响应后，自动激活本次新产生的测试记录

### 5.4 Response 面板的三个 Tab

#### Stream Frames（流式帧）
- 实时显示服务端推送的每一帧数据
- 按帧类型着色：start（蓝）、partial（黄）、final（绿）、error（红）
- 长文本自动换行，不会撑宽页面

#### Parsed Result（解析结果）
- 将 `streamContent` 中的 Python repr 格式解析为 JSON 并渲染为可交互的树形结构
- **关键词搜索**：顶部搜索框支持按关键词过滤和高亮节点
- **多选能力**：每个节点带复选框，支持父子联动选择
- 深层节点自动折叠，大型数组（>5项）默认收起

#### Build JSON（构建JSON）
- **Selected Items**：展示所有已选字段，支持逐个删除。
  - **Clear selected**：只移除累加的历史选择，保留当前 Parsed Result 中仍勾选的条目及其勾选状态。
  - **Restore parsed selections**：将当前 Parsed Result 中勾选的条目重新添加至此列表。
  - 上述两项操作均不会删除已构建的 JSON。
- **Quick Build**：根据选中内容智能生成结构化 JSON
  - 列表元素中的叶子节点会提升为最小父对象参与构建。例如选择 `dataCapabilities.1.id` 时，会使用包含 `id` 与 `description` 的完整能力项。
  - `Build dataCapabilityIds` — 从选中能力所在的完整列表元素提取 ID，符合 Schema 查询接口的入参格式。
  - `Build candidateAssetIds` — 从选中素材所在的完整列表元素提取 ID。
  - `Build candidateEventCandidates` — 从选中事件所在的完整列表元素构建事件候选。
  - `Build candidateDataBindings` — 从选中 Schema 所在的完整列表元素构建完整绑定结构。
  - `Build All as JSON` — 按原始层级构建 JSON 子集：选中数组项保持为数组元素，未选中的同级数组保留为空数组，不会产生 `"[1]"` 这类对象键。
  - 未选择字段时不显示无效的构建操作，提示先在 Parsed Result 中选择字段
- **Quick Transfer**：跨接口查看历史时出现，一键智能构建转换数据
- **Built JSON**：可编辑的 JSON 文本区域，支持手动调整
- **Apply to Form**：将构建的 JSON 推送到当前接口的表单字段，并保持在 Build JSON 视图
- **Copy JSON**：复制到剪贴板
- **Clear**：同时清空 Selected Items、Parsed Result 勾选状态与 Built JSON；如只需移除累加的历史选择，应使用 **Clear selected**

### 5.5 跨接口数据传递流程

这是本工具的核心特色功能，完整流程如下：

```
步骤1: 在 getWidgetCapabilityOverview 接口发送请求，获得能力概览
步骤2: 切换到 getDataCapabilitySchemas 接口
步骤3: 点击"其他接口历史"中的 Overview 历史记录
步骤4: Response 面板显示该历史的结果，出现 Transfer Banner
步骤5: 方式一：点击 Quick Transfer 按钮自动构建 dataCapabilityIds
       方式二：在 Parsed Result 中手动勾选需要的能力ID
步骤6: 在 Build JSON 中确认/编辑构建结果
步骤7: 点击 "Apply to Form" 推送到左侧表单
步骤8: 补充/修改参数后发送请求
```

**关键设计决策：**
- 点击其他接口历史时**不会自动推送数据**，而是先展示结果让用户选择
- 表单状态在接口切换时自动保存和恢复
- Apply to Form 在跨接口模式下**不会切换接口**，只填充当前接口的表单字段
- 点击 Send Request 后会解除此前历史记录的关联，并切换到 Stream Frames 显示本次响应；最终响应会作为新的当前测试记录

### 5.6 Preview JSON（请求预览）

- 点击 "Preview JSON" 弹出模态对话框（非 alert），显示格式化的完整请求 JSON
- 对话框内可直接全选复制
- 提供 "Copy to Clipboard" 按钮一键复制

---

## 6. 核心模块架构

### 6.1 JavaScript 模块划分

```
┌──────────────────────────────────────────────────┐
│                  MainController                   │
│  (初始化、事件绑定、请求构建、发送控制)             │
├──────────┬───────────┬───────────┬───────────────┤
│ Config   │ FormMgr   │ WsClient  │ ResponseRdr   │
│ 端点配置  │ 表单管理   │ WS连接    │ 响应渲染      │
├──────────┼───────────┼───────────┼───────────────┤
│ History  │ Selection │ TreeRdr   │ BuildMgr      │
│ Manager  │ Manager   │           │               │
│ 历史记录  │ 选择管理   │ 树形渲染  │ JSON构建      │
├──────────┼───────────┼───────────┼───────────────┤
│ DataTransfer        │ PythonReprParser           │
│ 跨接口数据转换       │ Python repr → JSON 解析    │
└─────────────────────┴────────────────────────────┘
```

### 6.2 关键模块职责

| 模块 | 职责 |
|------|------|
| **Config** | 存储三个接口的 WebSocket 端点路径 |
| **PythonReprParser** | 将服务端返回的 Python repr 格式字符串解析为 JSON 对象，使用状态机处理单/双引号、转义字符、Python 布尔值/None |
| **FormManager** | 管理三个接口的表单定义、渲染、数据收集、状态保存/恢复 |
| **WebSocketClient** | 封装 WebSocket 连接生命周期（连接、发送、接收、关闭） |
| **HistoryManager** | 管理请求历史记录，分当前接口和其他接口渲染，处理跨接口查看 |
| **SelectionManager** | 管理 Parsed Result 中的多选状态 |
| **TreeRenderer** | 将 JSON 对象渲染为可折叠、可搜索、可选择的树形结构 |
| **BuildManager** | 管理 Build JSON 面板，提供 Quick Build 和 Quick Transfer 功能 |
| **DataTransfer** | 核心转换逻辑，根据源接口→目标接口的组合，智能构建可传递的数据结构 |
| **ResponseRenderer** | 管理右侧 Response 面板的三个 Tab 视图 |
| **AppState** | 全局状态管理，跟踪当前选中的接口 |

---

## 7. 文件说明

### 7.1 目录结构

```
websocket_test_from_gui/
├── index.html              # 前端调试工具（单文件，约 1800 行）
├── input_output_schema.md  # 三个接口的完整输入输出 Schema 示例
└── README.md               # 本文档
```

### 7.2 相关文件（后端）

| 文件路径 | 说明 |
|----------|------|
| `cloud/start_websocket_server.py` | 服务入口，创建 FastAPI 应用并启动 Uvicorn |
| `cloud/api/routes.py` | WebSocket 路由定义，包含三个接口的路由和统一分发逻辑 |
| `cloud/api/schemas.py` | Pydantic 请求模型定义（CapabilityOverviewRequest 等） |
| `cloud/services/widget_generation_service.py` | 业务逻辑实现 |
| `cloud/config/config.py` | 服务配置（端口、主机等） |
| `cloud/test_compact_dsl_ws.py` | Python WebSocket 测试客户端示例 |

---

## 8. 使用指南

### 8.1 启动步骤

1. **启动后端服务：**
   ```bash
   cd D:\Workspace\GenUI\genui-agent\cloud
   python start_websocket_server.py
   ```

2. **打开调试工具：**
   在浏览器中直接打开 `websocket_test_from_gui/index.html`

3. **确认连接：**
   页面顶部 Server URL 默认为 `ws://127.0.0.1:8855`，确保后端服务已启动

### 8.2 典型调试流程

**场景：生成一个包含天气信息的通勤卡片**

1. 选择 `getWidgetCapabilityOverview` Tab，点击 Send
2. 在 Parsed Result 中查看返回的数据能力、事件能力、素材资源
3. 切换到 `getDataCapabilitySchemas` Tab
4. 点击"其他接口历史"中的 Overview 记录
5. 点击 Quick Transfer 自动构建 `dataCapabilityIds`，或手动勾选需要的能力
6. 点击 Apply to Form，参数自动填入表单
7. 点击 Send 获取 Schema 详情
8. 切换到 `generateWidgetCardCompactDsl` Tab
9. 从 Schemas 历史中 Quick Transfer 构建 `candidateDataBindings`
10. 从 Overview 历史中选取素材和事件
11. 填写 userQuery、title、description
12. 点击 Send 生成卡片

### 8.3 注意事项

- 工具需要在后端服务运行时才能发送请求
- `generateWidgetCardCompactDsl` 接口调用可能耗时较长（涉及模型调用），期间会收到心跳帧
- Python repr 解析器能处理大多数情况，但极端复杂的嵌套字符串可能解析失败，此时可参考 Stream Frames 中的原始内容
- 历史记录仅保存在内存中，刷新页面后会清空
