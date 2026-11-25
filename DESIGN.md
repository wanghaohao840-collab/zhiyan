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

- **自托管字体**：使用 `@fontsource-variable/inter` 仅打包拉丁子集 `inter-latin-wght-normal.woff2`（~47KB 同源预加载，零外链），中文优先回退系统原生黑体（`PingFang SC` / `Hiragino Sans GB` / `Microsoft YaHei`）。数字与时间戳统一启用 `tabular-nums`。
- **行高与字重**：UI 控件行高 `1.5`，QA / Notes / Search 长阅读区行高 `1.8`（最大行宽 `680px–720px`）；字重克制使用 `400 / 500 / 600`。
- **图标体系**：全站统一 `lucide-react` 线性图标，`strokeWidth={1.6}`，装饰图标显式声明 `aria-hidden="true"`。
- **共享原语**：统一使用 `web/src/components/ui/` 下的 `PageHeader`、`EmptyState`、`Skeleton`、`OverlayDialog`、`StatusBadge`、`ConstellationMark`、`ThemeSwitcher`；所有 5 类弹层/抽屉均收敛至 `OverlayDialog`。
- **动效工艺（融合 `emil-design-eng`）**：禁用 `transition: all`；交互过渡控制在 `140ms–200ms`；按钮按压提供 `scale(0.98)` 触觉确认；弹层严禁 `scale(0)`，统一柔和淡入与微位移；全局遵守 `prefers-reduced-motion: reduce`。
