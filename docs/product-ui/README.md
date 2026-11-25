# 知研产品 UI 工作流

本目录记录知研 React 产品界面的受支持设计、开发、验证与发布流程。当前已交付
认证流程、响应式应用外壳、文档库、智能问答、学习笔记、概览与学习洞察垂直切片；Memory、RAG、文档隔离、引用、
报告、批量导入与存储仍由现有 `ApplicationServices` 边界提供，不在 React 中
复制业务状态或后端逻辑。

## 当前范围与路由

| 路径 | 当前职责 |
|---|---|
| `/login`、`/register` | React 登录与注册；成功后恢复目标页或进入 `/overview` |
| `/overview` | 真实学习概览：可靠指标、最近文档和继续学习入口 |
| `/documents` | 真实文档库：列表、批量导入、任务进度、重试、取消与删除 |
| `/qa` | 真实智能问答：固定文档范围、持久对话、引用、摘要、重试与安全删除 |
| `/search` | 文献检索导航位；当前显示明确的迁移状态 |
| `/notes` | 真实学习笔记：Markdown 草稿与预览、版本化保存、来源、筛选、游标分页、冲突、投影重试与安全清空 |
| `/insights` | 真实学习洞察：近 30 天统计、报告生成/历史/查看及 Markdown/Word 下载 |
| `/api/v1/auth/*` | FastAPI 注册、登录、会话恢复与退出 API |
| `/legacy/` | 共享同一 `ApplicationServices` 的完整 Gradio 界面与回滚入口 |
| `/healthz` | 统一服务健康检查，返回 `{"status":"ok"}` |

迁移页不展示虚构产品数据，而是说明功能仍在迁移并提供 `/legacy/` 操作。后续
下一垂直切片为 search；每个切片应复用现有服务
边界，并在端到端能力完成后替换对应迁移状态。

## Penpot 连接与交接

2026-11-24 起，知研产品界面升级至 **Paper & Forest v2.0** 统一设计体系，2026-11-25 完成 **双气质实时切换体系（Style A · 极简知识感 `paper` ⇄ Style B · 深色 AI 感 `midnight`）** 以及 **P0 + P1 + P2 工程化与设计精进（v3）**：
- 仓库单一事实源为根目录 [`DESIGN.md`](../../DESIGN.md) 与支持 W3C DTCG `$modes` 双模的 [`design/tokens/zhiyan.tokens.json`](../../design/tokens/zhiyan.tokens.json)（经 [`scripts/design_tokens.mjs`](../../scripts/design_tokens.mjs) 编译为 [`web/src/styles/tokens.css`](../../web/src/styles/tokens.css)，新增双模 `--color-syntax-*` 语法高亮与 `--ease-out` / `--ease-drawer` 动效曲线）；
- 零外部 CDN：通过 `@fontsource-variable/inter` 自托管单一拉丁 WOFF2 子集（~48 KB）并裁剪自托管 OFL 中文衬线标题子集 `zhiyan-serif-subset.woff2`（`14.57 KiB`）；登录主图提供 `<picture>` AVIF/WebP/兜底三层自适应（瘦身 94.7%–96.6%）；
- 登录后 7 页采用 `React.lazy + Suspense` 路由级代码分割 + 悬停/聚焦预取 + 精细 `manualChunks` 拆包（首屏业务入口降至 `~40 KB`，零 `> 500 KB` chunk 告警）；
- 全站交互补齐：QA 正文内联 `[1]` 引用直达右侧 `SourcePanel` 滚动+脉冲高亮、对话头绑定文档范围可展开胶囊、输入框自动撑高与长对话智能贴底/回底胶囊；Markdown 接入自托管 `KaTeX` 数学公式、双模语法高亮与带语言顶栏的一键复制按钮；`/qa` 与 `/notes` 三栏可拖拽调宽；全局 `⌘K / Ctrl+K` 命令面板、非阻塞 Toast 栈（支持任务完成一键撤销）、`OverlayDialog` 物理入场动效、View Transitions API 圆形扩散切主题、`150ms` 防闪骨架屏、全局双主题 `ErrorBoundary` 与 `OfflineBanner`；
- 全站固定像素基线保持 58 张（`visual.spec.ts` 22 张锁定契约基线、`qa.spec.ts` 8 张、`notes.spec.ts` 4 张、`pages-baselines.spec.ts` 12 张、`theme-dual-mode.spec.ts` 12 张，覆盖 AVIF 命中、`Ctrl+K`、QA 范围胶囊、KaTeX 与代码高亮以及 Search/Notes/Insights/Learning 四页 Style B 的 Axe 与 `320px` 零溢出）；
- 旧 Penpot 画板仅保留历史参考与导出映射契约，未同步改动远端画板。

（历史记录：2026-09-10 浅色界面视觉依据为用户确认的左侧参考图及
[浅色知识感设计](../superpowers/specs/2026-09-09-light-knowledge-ui-design.md)。）

以下为保留的 Penpot 历史交接流程。使用已登录的浏览器会话打开规范化文件
链接，不把登录 Cookie、`userToken`、API Key 或其他凭据复制到命令、日志、
截图、提交或文档中。仓库只保存 team/file 标识组成的无密钥链接。发布核验只读
取回文件、页面、组件和画板；除非另有明确设计任务，不在发布门禁中写入 Penpot。

文件名、无密钥 URL、七个页面 ID、组件 ID、七个参考画板 ID 与导出路径见
[`penpot-handoff.md`](penpot-handoff.md)。React 对应关系保存在
[`penpot-component-map.json`](penpot-component-map.json)，不得用名称猜测或替换
已经 fresh-read 核验的 ID。

Login 的 Penpot 权威参考覆盖 desktop `1440 × 1024`、tablet `1024 × 768` 和
mobile `390 × 844`。三档设计均不再展示“保持登录状态”；TextField 与
PasswordField 的内部输入层按父表单宽度填充，PasswordField 的可见性按钮保持
`44 × 44`。仓库参考图必须由这三个真实画板直接导出，不得以浏览器截图代替。

## DTCG Token 与组件映射

[`design/tokens/zhiyan.tokens.json`](../../design/tokens/zhiyan.tokens.json) 是仓库内
受版本控制的 DTCG Token 契约，
[`web/src/styles/tokens.css`](../../web/src/styles/tokens.css) 是生成物，不应手工
维护。视觉值按本轮已批准设计更新 DTCG 快照并重新生成 CSS；旧 Penpot 映射单独保留：

```powershell
node scripts/design_tokens.mjs design/tokens/zhiyan.tokens.json web/src/styles/tokens.css
node scripts/design_tokens.mjs --check design/tokens/zhiyan.tokens.json web/src/styles/tokens.css
Set-Location web
npm ci
Set-Location ..
node --test tests/design/test_design_tokens.mjs tests/design/test_penpot_component_map.mjs tests/design/test_penpot_handoff.mjs
```

组件映射测试从 `web/node_modules` 加载 lockfile 固定的 Ajv 依赖，因此首次检出或
`web/package-lock.json` 变更后必须先运行紧邻测试命令的 `npm ci`。最后一条命令
同时验证 DTCG、映射 schema、无密钥 URL、组件 ID 唯一性、代码文件存在性和
`verified` fresh-read 标记。映射只覆盖公开 React 组件契约，不改变 Penpot 文件
本身。

## 本地开发

使用项目正式 venv，不使用系统 Python 或 Anaconda。先在仓库根目录启动后端：

```powershell
.\venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 7860 --workers 1
```

再在另一个 PowerShell 中启动前端：

```powershell
Set-Location web
npm run dev
```

Vite 将 `/api` 和 `/legacy` 代理到 `http://127.0.0.1:7860`。首次检出或 lockfile
变更后的依赖安装已在上一节的验证准备中完成；普通前端迭代可直接运行
`npm run dev`。

## 统一生产进程与 Docker

生产构建先生成 `web/dist`，然后由一个 Uvicorn worker 同时提供 API、React SPA
和挂载在 `/legacy` 的 Gradio：

```powershell
Set-Location web
npm ci
npm run build
Set-Location ..
.\venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 7860 --workers 1
```

Session、用户写锁和导入 worker pool 都是进程内资源；多 Uvicorn worker 或同时
启动第二个 Gradio 进程会分裂状态，因此不受支持。容器镜像使用多阶段构建并以
非 root 用户运行：

```sh
docker build -t zhiyan:local .
docker compose --env-file deploy/.env up -d --build
```

Compose 的秘密与运行数据约束、Qdrant/Neo4j 配置、备份和恢复流程见
[`deploy/README.md`](../../deploy/README.md)。不得提交 `deploy/.env`、运行数据库、
上传文档或报告。

## `/qa` 发布、恢复与演进

`/qa` 默认启用。只有环境值精确为 `false`（忽略大小写）时，
`QA_ROUTE_ENABLED=false` 才会在重启后把产品路由切回明确的迁移状态；该开关不
停止问答迁移、启动恢复、摘要 worker 或删除 worker，也不会恢复对旧
`history.json` 的双写。需要表现层回滚时先关闭路由并重启单一服务，再按下节使用
`/legacy/`；旧界面的新问答仍必须经过同一 `QaService`，不能形成第二份历史。

浏览器把 pending 消息、摘要任务和删除任务视为服务端资源，每 `1500 ms` 轮询
活动状态。刷新、断网重连或重新登录后，页面重新读取会话、消息、任务和删除状态，
而不是依赖内存中的动画或 token 流。一次用户动作保持稳定的
`client_request_id`；仅安全、可重试的请求允许客户端自动重试一次。服务启动时会
回收过期摘要/删除租约，并把失去执行者的同步 pending 回答收敛为安全失败，供用户
显式重试。

- 对话列表每次读取 20 条，消息首屏读取最新 50 条；用户可显式加载更多对话或更早消息。
- 摘要任务由服务端持久化；刷新或重新进入对话后，客户端通过活动任务资源恢复进度与取消入口。
- 浏览器存储不是任务或历史事实源；现有 1500 ms 轮询可在后续替换为 SSE/WebSocket，而资源 ID 与恢复读取保持兼容。

冷备份必须把 `app.db` 与用户目录、Memory、Qdrant（以及启用时的 Neo4j）作为一
致集合处理，使用 `deploy/backup.sh`/`deploy/restore.sh`，不得只复制 QA 表。删除
会话会先建立栅栏并立即停止读取/生成，再由可恢复 worker 清理消息、引用、摘要、
任务和关联 QA Memory；文档删除还会级联引用它的会话。旧迁移源或回滚副本不得以
“兼容”为由保留已删除内容。

当前 `1500 ms` 轮询是传输策略，不是领域模型。后续可在保持现有资源 ID、版本、
幂等请求和状态端点的前提下替换为 SSE/WebSocket 通知。多副本部署前仍必须完成共享
Session、分布式用户锁、共享任务队列/唤醒和一致存储；在这些条件满足前继续保持单
应用副本、单 Uvicorn worker，不能仅提高 `--workers`。

## `/notes` 发布、恢复与数据契约

`/notes` 默认启用；只有环境值精确为 `false`（忽略大小写）时，
`NOTES_ROUTE_ENABLED=false` 才隐藏 React 入口并显示迁移状态。这个开关只控制
访问表现，**不**停止 legacy Note 迁移、启动恢复或投影恢复。重新启用后，用户仍从
同一持久化 Note 聚合读取数据。

- API 与 SQLite/FTS5 是 Note 的唯一事实源；Memory 只是异步、可重建的投影。投影
  失败不能丢失、回滚或阻塞已保存的 Note，用户可显式重试。
- `history.json` 的 legacy Notes 迁移是幂等的，迁移完成后新 Note 不再双写 legacy
  History 或随机 Memory；稳定投影才按精确 Note ID 清理对应旧投影。
- QA “记为笔记”与“记录此引用”只把稳定的 QA/citation ID 带入内存草稿。用户必须
  显式保存；服务端在同一用户和删除栅栏内解析来源，绝不接受正文或摘录通过 URL 传递。
- 清空是用户作用域内的 Note 软删除：活动列表清空而 tombstone 留存；文档和 QA
  记录不受影响。来源文档删除时，Note 的用户正文保留，来源展示改为“来源已删除”且
  不保留旧摘录。
- 当前相同用户写锁、投影 worker 与运行时注册表都是进程内资源，受支持拓扑仍是一个
  应用进程、一个 Uvicorn worker。Docker 与本地部署都不得增加 worker 来规避该限制。

## `/search` 本地文档检索与来源笔记

实现已集成，生产发布验收状态见
[`检索发布记录`](../agent-workflow/task-packets/2026-09-04-document-search/04-release-integration.md)。
“文献检索”当前仅检索用户已导入的本地文档，不包含互联网论文发现或外部文献数据库。

- 显式选择 1–10 篇文档，输入去除首尾空白后 1–1000 字符的问题；每次返回 5/10/20
  条窗口内结果。展示数量不是全库命中总数，检索分数也不是答案置信度。
- 来源详情、引用复制、问答范围确认和保存前可编辑笔记贯通桌面、平板、手机。
  页码/章节仅在原数据存在时展示；无数据时不推测。没有检索历史或联网搜索。
- 查询、摘录、待保存草稿只存在当前会话的内存中，不写入 URL 或浏览器持久存储。
  范围/查询/用户变化后旧结果不可继续操作；晚到响应不能覆盖新请求或新用户状态。
- 保存笔记仅提交稳定片段定位与校验和；服务端重新验证同用户文档、片段原文及删除栅栏。
  用户编辑正文与来源快照分开存储。文档删除后来源定位/摘录被清理，笔记正文保留。
- 来源扩展使用新增 `note_document_sources` 表，不改写旧 QA 来源表。旧程序可读取旧表
  **不代表**旧程序能正确清理新来源数据。部署回退必须使用安全更新器配对的旧镜像和
  一致性冷备份恢复，不得只切旧镜像继续写新数据库。
- 本轮没有修改或重新验收远端 Penpot 源文件；三档实现与仓库设计契约已通过隔离测试。
  分布式共享会话、用户锁和任务协调仍是未来发布的前置工作，不增加应用副本数。

## Cookie 与 CSRF

- 服务端把 session token 放在 `zhiyan_session` Cookie 中，属性为 `HttpOnly`、
  `SameSite=Lax`、`Path=/`；React 不读取 Cookie，也不把 session 或 CSRF 数据
  写入 `localStorage`/`sessionStorage`。
- 浏览器请求使用 `credentials: "same-origin"`。登录、注册与会话恢复响应只把
  username 和 CSRF token 交给内存中的认证状态，不返回 session token、用户 UUID
  或数据路径。
- 已认证的 `POST`、`PUT`、`PATCH`、`DELETE` 请求必须由客户端添加
  `X-CSRF-Token`；缺失或错误 token 返回 `403 invalid_csrf_token`。退出登录也遵循
  此规则，成功后服务端失效会话并清除 Cookie。
- 本地明文 HTTP 保持 `APP_COOKIE_SECURE=false` 或不设置。经过 HTTPS 网关的生产
  部署必须设置 `APP_COOKIE_SECURE=true`，使 Cookie 带 `Secure`；反向代理还必须
  保持同源路由语义。

## `/legacy` 回滚窗口

第一阶段发布期间保留 `/legacy/`，供尚未迁移的业务能力和 React 外壳故障时
回滚。回滚是把用户导向同一统一服务的 `/legacy/`，不是启动第二个进程；该挂载
共享同一 `ApplicationServices`、会话注册表和 worker pool。若 React 路由出现
问题，先确认 `/healthz` 正常，再直接使用 `/legacy/`，保留现场并回退相关前端
提交或镜像。不要为回滚提高 worker 数或复制数据目录。

## 视口、可访问性与截图基线

Playwright 固定三个目标视口：desktop `1440 × 1024`、tablet `1024 × 768`、
mobile `390 × 844`。认证、导航、键盘焦点与响应式行为在这些项目中验证；axe
要求 WCAG 2 A/AA 与 2.1 A/AA 下没有 serious/critical violation。

应用外壳基线位于 `web/e2e/visual.spec.ts-snapshots/`（22 张锁定契约），QA 八态基线位于
`web/e2e/qa.spec.ts-snapshots/`（8 张），Notes 基线位于 `web/e2e/notes.spec.ts-snapshots/`（4 张），
概览/检索/洞察/学习中心三视口基线位于 `web/e2e/pages-baselines.spec.ts-snapshots/`（12 张），
Style B 深色 AI 感与双主题切换基线位于 `web/e2e/theme-dual-mode.spec.ts-snapshots/`（12 张，含笔记深色 Markdown 预览）。截图禁用动画、隐藏插入光标并
启用 reduced motion。只有 Penpot 批准的视觉变化或经过审阅的有意实现变更才可
运行 `npx playwright test --update-snapshots`；更新前后必须以匹配视口对照
[`reference/penpot/`](reference/penpot/)，逐张检查布局、换行、状态和裁切。不要
用整块遮罩掩盖差异，也不要因为测试失败批量接受新基线；仅可遮罩不可避免的
动态值。失败产生的 `web/test-results`、报告、trace 和临时数据不得提交。

完整前端发布检查为：

```powershell
Set-Location web
npm ci
npm test
npm run typecheck
npm run lint
npm run build
npx playwright test
Set-Location ..
```

## 闭环验收记录

本轮依赖、认证语义、Penpot 源文件、三档视觉与 Docker Linux 发布门的可复现结果见
[产品 UI 闭环报告](closure-report-2026-08-09.md)。报告中的隔离 Compose 项目只用于验收，
不会替换或停止现有 `7860` 环境。
