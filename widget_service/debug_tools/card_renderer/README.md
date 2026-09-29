# 卡片生成结果渲染器

这是调试平台的本地卡片预览子包，负责把生成链路输出的 JSONL/JSON 转成可交互的 Web 预览。实现从 `docs/index.html` 抽取的解析和样式映射，不承担 HarmonyOS 端侧校验、模型重试、截图或测评。

## 支持输入

- A2UI v0.9：`createSurface`、`updateComponents`、`updateDataModel` 对象行；
- Compact DSL：`[id, component, props, children]` 行和 DataModel 路径行；
- Design Compact DSL：在 Compact DSL 上增加 `design` token；
- JSONL、连续 JSON 值，以及包含 `genui`/`cardSpec`/`artifact` 字段的常见 artifact 外壳。

解析器会处理 DataModel 路径、`{{ ${/path} }}` 模板、简单拼接/条件表达式、`formatString`，并推断 2×2（160×160）和 2×4（320×160）画布尺寸。相对图片资源以 `/resources/` 为默认根目录，加载失败时显示与资源语义相关的内置 SVG 回退图标。

## 导出

```tsx
import {
  CardRenderer,
  CardPreview,
  parseInput,
  type RendererDocument,
} from '@widget-debug/card-renderer';

<CardRenderer
  initialValue={artifact.genui}
  assetBaseUrl="/resources/"
  onArtifact={(document: RendererDocument) => store.publish(document)}
/>
```

`CardPreview` 用于统一工作台已经有输入编辑器的场景；`parseInput` 是无 DOM 的纯函数，便于接口调试结果导入、单元测试和状态检查。

## 独立运行

```bash
npm install
npm run dev
npm run typecheck
npm run build
```

生产环境可将 `frontend/static` 的构建产物交给统一 FastAPI `/debug/` 静态入口；图片根目录通过 `assetBaseUrl` 配置，避免依赖迁移后的相对路径。

