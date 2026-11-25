# 知研（ZHIYAN）设计系统规范 — 双气质 v2.0（Style A 极简知识感 × Style B 深色 AI 感）

> **单一事实源（SSOT）**：本文档与 [design/tokens/zhiyan.tokens.json](design/tokens/zhiyan.tokens.json) 保持单源同步，通过 [scripts/design_tokens.mjs](scripts/design_tokens.mjs) 编译为 [web/src/styles/tokens.css](web/src/styles/tokens.css)（含 `:root` 与 `:root[data-theme="midnight"]` 双模变量）。所有业务样式禁止硬编码色值或非 Token 阴影。

---

## 1. 产品设计哲学

知研是以 **Memory + RAG** 为核心的中文智能文档学习与研究工作台。界面提供两套可一键实时切换、共享同一套清晰信息架构与无障碍标准的视觉气质：

- **Style A · 极简知识感（`data-theme="paper"`，默认）**：*如纸页般宁静，如思想般深远* —— 温润宣纸底（`#F7F6F2`）+ 纯净白纸卡（`#FFFFFF`）+ 沉稳森林深绿（`#1F6851` / `#16503E`）+ 水墨浮空书页主视觉，绿系强调占比严控在 8%–10% 以内，服务长时间深度阅读与写作。
- **Style B · 深色 AI 感（`data-theme="midnight"`）**：*在深邃中思考，在连接中生长* —— 极深午夜蓝黑底（`#070C15`）+ 精密玻璃卡片（`#0E1624` 配 `inset 0 1px 0 rgba(255,255,255,0.06)` 顶边折射光与 `1px solid rgba(148,163,184,0.16)` 发丝边框）+ 冰蓝辉光（`#38BDF8` / `#7DD3FC`）+ 高对比纯白主行动按钮（`#F1F5F9` 配 `#09111F` 深墨字）+ 深空日蚀光环主视觉。
- **数据诚实性**：所有统计数字、活动趋势、引用来源和任务状态均严格来自后端 `/api/v1` 真实返回，严禁虚构演示指标或假思考文本。

---

## 2. 色彩系统（全组合通过 WCAG 2.1 AA）

| 语义变量 | Style A（`paper`） | Style B（`midnight`） | 用途 |
|---|---|---|---|
| `--color-canvas` | `#F7F6F2` | `#070C15` | 全局画布背景 |
| `--color-surface` | `#FFFFFF` | `#0E1624` | 主卡片、侧栏、阅读区 |
| `--color-surface-subtle` | `#F2F1EB` | `#142034` | 内嵌输入框、代码块、引用摘录底、步骤槽 |
| `--color-surface-brand` | `#E8F3EC` | `rgba(56, 189, 248, 0.14)` | 激活导航胶囊、选中卡片、状态底色 |
| `--color-text-primary` | `#1C231F` | `#F1F5F9` | 主标题、核心指标、正文（均 ≥ 15:1） |
| `--color-text-secondary` | `#545E57` | `#94A3B8` | 副标题、辅助说明、元信息（均 ≥ 5.9:1） |
| `--color-text-muted` | `#657068` | `#7E8FA6` | 占位符、次要时间戳（均 ≥ 4.7:1） |
| `--color-border-subtle` | `#E5E4DC` | `rgba(148, 163, 184, 0.16)` | 卡片发丝边框与分割线 |
| `--color-border-control` | `#879088` | `#4A5F7D` | 输入框、复选框、下拉控件边界（均 ≥ 3.1:1） |
| `--color-brand-600` / `-700` | `#1F6851` / `#16503E` | `#38BDF8` / `#7DD3FC` | 图表主线、图标、链接与高亮强调 |
| `--color-primary-cta-bg` / `-text` | `#1F6851` / `#FFFFFF` | `#F1F5F9` / `#09111F` | 主操作按钮（登录、发送、导入、新建） |
| `--color-focus` | `#16503E` | `#38BDF8` | 全站统一 `2px solid + 2px offset` 焦点环 |

---

## 3. 排版、字体、图标与动效工艺

- **自托管双字体（零外链 CDN）**：
  - 拉丁无衬线：通过 `@fontsource-variable/inter` 仅打包拉丁可变字体子集 `inter-latin-wght-normal.woff2`（~48 KB 同源预加载）；
  - 中文衬线标题：从开源 OFL `Noto Serif SC` 精确裁剪核心品牌与展示标题汉字为 `zhiyan-serif-subset.woff2`（`14.57 KiB`，`< 15 KB`），通过 `--font-serif` 优先用于登录主标语与哲学箴言；正文中文回退系统原生无衬线体，所有数字与时间戳统一启用 `tabular-nums`。
- **图像与包体优化**：登录/注册主视觉通过 `<picture>` 按浏览器能力渐进提供自托管 `AVIF`（Style A `49.36 KB` / Style B `22.97 KB`）→ `WebP` → 兜底 `PNG/JPG`；登录后 7 个工作台页面全面接入 `React.lazy + Suspense` 路由级代码分割与侧栏/底栏 `onMouseEnter / onFocus` chunk 预取，并拆分为 `react-vendor` / `query-vendor` / `icons-vendor` / `katex-vendor` / `highlight-vendor` / `markdown-vendor`（首屏业务入口 ~40 KB）。
- **Web Interface Guidelines 排版与移动端兜底**：
  - 选中态粗体切换的导航项与标签栏使用 `data-text` + `::after` 隐形伪元素预占 `font-weight: 600` 宽度，零横向抖动；
  - `user-select: none` 严格仅限定于短标签交互 Chrome（侧栏/底栏导航、按钮、分段切换、标签页、拖拽分隔手柄），文章正文、引用、代码与错误提示均保持可自由划词复制；
  - `< 768px` 移动端交互表单统一 `font-size: max(1rem, 16px)` 杜绝 iOS Safari 自动放大，同时绝不限制 `user-scalable` / `maximum-scale`。
- **行高与字重**：UI 控件行高 `1.5`，QA / Notes / Search 长阅读区行高 `1.8`（最大行宽 `680px–720px`）；字重克制使用 `400 / 500 / 600`。
- **图标体系**：全站统一 `lucide-react` 线性图标，`strokeWidth={1.6}`，装饰图标显式声明 `aria-hidden="true"`。
- **共享原语**：统一使用 `web/src/components/ui/` 下的 `PageHeader`、`EmptyState`、`Skeleton`（含 `150ms` 防闪显现与 `useDelayedBusy`）、`OverlayDialog`、`StatusBadge`、`ConstellationMark`、`ThemeSwitcher`、`ToastProvider`、`CommandPalette`（全局 `⌘K / Ctrl+K` 0ms 瞬开）、`PaneResizer`（`/qa` 与 `/notes` 三栏可拖拽与键盘调节）、`ErrorBoundary`、`OfflineBanner`。
- **Markdown 渲染与代码/公式工艺**：`MarkdownPreview` 基于 `rehype-sanitize` 严格白名单后挂载自托管 `KaTeX` 数学公式与 `rehype-highlight` 双模语法高亮（全部映射至 `--color-syntax-*` DTCG Token，零硬编码），代码块带语言顶栏与 `Copy → Check` 160ms 形变一键复制按钮及 Toast 反馈；QA 回答支持正文内联 `[1]` 上标点击直达右侧 `SourcePanel` 自动滚动与脉冲高亮。
- **动效工艺（融合 `emil-design-eng`）**：禁用 `transition: all`；交互过渡控制在 `140ms–200ms`；按钮按压提供 `scale(0.98)` 触觉确认；`OverlayDialog` 模态使用 `scale(0.96)` + `cubic-bezier(0.23, 1, 0.32, 1)` `180ms`，抽屉/Sheet 使用 `cubic-bezier(0.32, 0.72, 0, 1)` `220ms`；主题切换在支持的浏览器启用 View Transitions API 圆形扩散过渡（`clip-path: circle(...)`），不支持或开启 `prefers-reduced-motion: reduce` 时自动无缝降级。
