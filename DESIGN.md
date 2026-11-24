# ZHIYAN / 知研 — DESIGN.md (Paper & Forest v2.0)

> **定位**：Premium AI Knowledge Workspace（基于 Memory + RAG 的中文智能文档学习与研究工作台）。  
> **设计隐喻**：一张整洁、可长期使用的数字研究桌——左侧是秩序，中心是内容，右侧是证据。  
> **单一事实源**：本文件与 [`design/tokens/zhiyan.tokens.json`](design/tokens/zhiyan.tokens.json) 构成仓库内视觉契约；[`web/src/styles/tokens.css`](web/src/styles/tokens.css) 为编译生成物，严禁手工覆写。

---

## 1. 视觉原则与反模板禁令（Anti-AI-Slop）

1. **三层暖纸层次**：画布 `#F8F7F3` → 主表面 `#FFFFFF` → 次级暖石 `#F3F2EC`，仅用 1px 暖灰发丝边建立秩序，严禁卡片层层嵌套。
2. **克制色相比例**：约 85% 暖纸与暖灰中性色、≤10% 森林绿品牌色、≤5% 低饱和语义状态色。严禁紫蓝渐变、霓虹发光、重玻璃拟态或装饰性图标彩虹色。
3. **克制字重与中文可读性**：正文 `400`，标题与强调 `500–600`，避免大面积 `700` 粗黑块。区分 UI 行高（`1.5`）与长文阅读行高（`1.75–1.85`），阅读列最大宽度控制在 `680–720px`。
4. **真实数据铁律**：所有统计、文档状态、任务进度、引用页码与学习任务必须来自 `/api/v1` 真实响应，绝不伪造 Memory 统计、知识图谱占位、模拟步骤或虚假置信度。
5. **零硬编码色值**：业务 CSS 严禁绕过 `var(--color-*)` / `var(--shadow-*)` / `var(--radius-*)` / `var(--font-*)` 写裸 hex 或自定义 rgba。

---

## 2. 色彩系统（全组合通过 WCAG 2.1 AA 校验）

| 角色 | CSS 变量 | 色值 | 对比度与用法约束 |
|---|---|---|---|
| 全局暖纸画布 | `--color-canvas` | `#F8F7F3` | 全站底色，替代冷白刺眼背景 |
| 主内容表面 | `--color-surface` | `#FFFFFF` | 卡片、阅读区、侧栏、弹层 |
| 次级暖石面 | `--color-surface-subtle` | `#F3F2EC` | 侧栏次区、代码块、摘录底、表头 |
| 品牌浅绿洗色 | `--color-surface-brand` / `--color-brand-100` | `#E8F2EC` | 激活导航、选中文档、命中高亮（其上文字必用 `--color-brand-700`） |
| 主文字 | `--color-text-primary` | `#202621` | 暖纸 `14.40:1` / 白底 `15.43:1` |
| 次级文字 | `--color-text-secondary` | `#576058` | 暖纸 `6.09:1` / 暖石 `5.82:1` / 白底 `6.52:1` |
| 弱化元数据 | `--color-text-muted` | `#677068` | 白底 `5.13:1` / 暖纸 `4.78:1`，用于占位与时间戳 |
| 发丝分割线 | `--color-border` / `--color-border-subtle` | `#E4E3DB` | 卡片边框与分割线（不可单独作为表单输入边框） |
| 控件边界 | `--color-border-control` | `#8A9188` | 白底 `3.24:1` / 暖纸 `3.02:1`，专用于输入框、下拉、复选框 |
| 森林绿主色 | `--color-brand-600` | `#24745B` | 主按钮背景（白字 `5.64:1`）、关键图标 |
| 深森林绿 | `--color-brand-700` | `#1B5B47` | 主按钮悬停（`7.97:1`）、浅绿底文字（`6.96:1`）、链接 |
| 极深森林绿 | `--color-brand-800` | `#144838` | 主按钮按下态 |
| 成功 / 浅底 | `--color-success` / `--color-success-subtle` | `#236E52` / `#E8F2EC` | 已完成 / 已导入状态 |
| 警告 / 浅底 | `--color-warning` / `--color-warning-subtle` | `#8F5A12` / `#FBF3E4` | 排队 / 等待重试 / 警告（浅底对比 `5.23:1`） |
| 危险 / 浅底 | `--color-danger` / `--color-danger-subtle` | `#B43E3E` / `#FBECEC` | 失败 / 错误 / 删除确认（浅底对比 `4.95:1`） |
| 信息 / 浅底 | `--color-info` / `--color-info-subtle` | `#38678A` / `#EDF4F9` | 处理中 / 运行态（浅底对比 `5.44:1`） |
| 焦点环 | `--color-focus` | `#1B5B47` | 全局 `:focus-visible` 统一 `2px solid` + `2px offset` |

---

## 3. 排版、间距、圆角与阴影

- **无衬线主体**：`--font-sans: Inter, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif`（零延迟系统中文栈前置，不阻塞远程 CJK 字体）
- **情绪衬线点缀**：`--font-serif: "Noto Serif SC", "Source Han Serif SC", "Songti SC", Georgia, serif`（仅登录宣言、空态引导语等极少情绪锚点）
- **等宽技术元信息**：`--font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace`（引用编号、页码、代码块、文件 ID）
- **数字指标**：统计数字与表格数字启用 `font-variant-numeric: tabular-nums`
- **圆角**：图标容器 `6px`、按钮与输入框 `10px`、标准卡片 `14px`、特色卡/弹层 `18px`、标签/徽章 `999px`
- **阴影**：卡片 `--shadow-card`（轻微 1px 层级），浮层 `--shadow-floating`，弹窗 `--shadow-dialog`
- **动效**：`140ms`（hover/button）、`180ms`（dialog/tab）、`200ms`（drawer），全部遵循 `prefers-reduced-motion: reduce`

---

## 4. 全站 7 + 2 路由与布局约束

- **应用外壳**：桌面侧栏默认 `228px`（支持折叠为 `68px` 图标轨），平板 `72px` 图标轨，移动端 `64px` 固定底栏（概览、文档、问答、检索、更多抽屉，含笔记、洞察、学习中心与退出登录）。
- **容器宽度分层**：
  - 阅读与管理页（`/overview`、`/documents`、`/insights`、`/learning`）：最大宽度 `1200px`
  - 三栏研究工作台（`/qa`、`/notes`、`/search`）：流式最大宽度 `1400px`，同时锁定正文阅读列 `max-width: 720px`
- **认证页**（`/login`、`/register`）：桌面暖纸双栏，保留真实认证、密码显隐与无依据状态清理。
- **触控与键盘**：所有交互目标在移动端至少 `44 × 44px`，所有弹层支持焦点陷阱、`Escape` 关闭与触发点焦点恢复。

---

## 5. 组件契约与工作流

- 不破坏 [`docs/product-ui/penpot-component-map.json`](docs/product-ui/penpot-component-map.json) 锁定的 10 个导出契约（`Button`、`TextField`、`AppShell`、`Sidebar`、`MobileBottomNav`、`DocumentRow`、`ImportTaskRow`、`FilePicker`、`QaDrawer`、`QaDeleteDialog`）。
- 共享原语位于 `web/src/components/ui/`（`StatusBadge`、`EmptyState`、`ConstellationMark`），图标统一基于 `lucide-react`（`strokeWidth={1.6}`）。
- 修改 Token 后必须执行：
  ```powershell
  node scripts/design_tokens.mjs design/tokens/zhiyan.tokens.json web/src/styles/tokens.css
  node scripts/design_tokens.mjs --check design/tokens/zhiyan.tokens.json web/src/styles/tokens.css
  ```
