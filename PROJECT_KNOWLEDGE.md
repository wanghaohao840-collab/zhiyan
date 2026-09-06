# 智能文档学习助手：项目基础知识

> 来源：项目历史 ChatGPT 共享对话（约 3.5 万字）的整理。共享链接：<https://chatgpt.com/share/6a37c6e2-b9bc-83ea-bc04-e601a7d85a75>
>
> 本文用于后续开发的背景知识，不是当前实现的绝对事实。若本文、旧对话与当前代码或运行结果冲突，以当前代码、测试、配置和运行结果为准，并及时更新本文。

## 0. 代码仓库与同步约定

- GitHub 仓库：<https://github.com/wanghaohao840-collab/->
- 默认分支：`main`
- 完成功能修改后，应先检查差异并运行相应验证，再提交和推送到该仓库。
- 每次交付时明确说明：改动文件、验证结果、是否已提交、是否已推送；推送失败时保留本地提交并说明原因。
- 未经用户明确授权，不提交 `.env`、API Key、令牌、个人数据、运行缓存、上传文档或生成报告等敏感/运行时文件。

### 固定运行环境

- 项目实际测试和启动环境是仓库内的 `venv`，不要直接使用系统 Python、Anaconda 或其他解释器。
- 验证命令：`.\venv\Scripts\python.exe -m pytest -q`
- 启动命令：`.\venv\Scripts\python.exe .\ui\gradio_app.py`
- 当前已验证：Gradio 6.19.0、python-docx 1.2.0、pytest 8.4.1，且 `pip check` 无冲突。

## 1. 项目定位

本项目是一个基于 **Memory + RAG** 的智能文档学习助手，而不只是大模型 API 或 PDF 问答 Demo。目标是围绕用户文档和学习过程形成完整闭环：

- 导入、解析和管理学习文档；
- 基于文档检索、问答、总结并给出来源；
- 保存学习笔记、问答记录和学习历史；
- 提供记忆回忆、学习统计和学习报告；
- 通过 Gradio 提供可操作、可演示的界面。

当前规划支持 PDF、TXT、Markdown 和 DOCX。PDF 应保留页码来源，其他格式至少保留文件名和 `document_id`。

## 2. 为什么采用当前架构

- **RAG**：解决模型不知道本地文档内容、回答可能无依据的问题。标准链路为“解析 → 切块 → 嵌入 → 存储 → 检索 → 构造上下文 → LLM 回答 → 来源展示”。
- **Memory**：记录用户学过、问过和记过的内容，弥补 RAG 只面向文档知识、不了解学习过程的不足。
- **Gradio**：快速构建文件上传、问答、检索、笔记、报告和文档管理界面，适合原型与项目展示。
- **本地 JSON 优先**：先低成本验证完整功能闭环，再升级 Qdrant、Neo4j 等外部基础设施。

## 3. 模块职责与依赖边界

| 模块 | 主要职责 |
|---|---|
| `hello_agents/memory/base.py` | `MemoryItem`、`MemoryConfig`、记忆抽象结构 |
| `hello_agents/memory/manager.py` | 统一协调各类 Memory 的添加、检索、遗忘和整合 |
| `hello_agents/memory/embedding.py` | 统一嵌入接口及本地/远程嵌入实现 |
| `hello_agents/memory/types/working.py` | 短期工作记忆及 TTL 等策略 |
| `hello_agents/memory/types/episodic.py` | 事件、会话与历史经历 |
| `hello_agents/memory/types/semantic.py` | 事实、概念和知识记忆 |
| `hello_agents/memory/types/perceptual.py` | 图片、语音等感知或多模态信息 |
| `hello_agents/memory/storage/document_store.py` | 原文、chunk 文本及元数据存储 |
| `hello_agents/memory/storage/qdrant_store.py` | Qdrant collection、向量写入、过滤检索和删除 |
| `hello_agents/memory/storage/neo4j_store.py` | 实体、关系及知识图谱存储 |
| `hello_agents/memory/rag/document.py` | 文档、chunk、元数据及文档处理 |
| `hello_agents/memory/rag/pipeline.py` | RAG 全流程调度与当前 JSON 持久化 |
| `hello_agents/tools/builtin/memory_tool.py` | 面向 Agent 的 Memory 工具适配层 |
| `hello_agents/tools/builtin/rag_tool.py` | 面向 Agent 的 RAG 工具适配层 |
| `assistants/pdf_learning_assistant.py` | 组合 MemoryTool 与 RAGTool，封装学习助手用例 |
| `ui/gradio_app.py` | 界面、事件绑定、状态展示及下拉框刷新 |

依赖方向应保持为：**UI → Assistant → Tool → Memory/RAG/Storage**。底层 Memory 或 Storage 不应反向导入 Tool，避免循环导入。

## 4. 已形成的核心产品规则

1. **多文档必须隔离**：导入时生成并保存 `document_id`；问答、检索、删除均传递并过滤当前 `document_id`，不能串文档。
2. **来源必须可追溯**：PDF 按页读取并在 chunk metadata 中保存 `page_number`；检索与回答显示文件名、页码和相关度。
3. **切块优先保持语义**：优先按段落切分，使用 `chunk_size` 控制长度、`chunk_overlap` 保留上下文。
4. **总结问题区别处理**：普通 top-k 检索不适合全文总结。检测“总结、主要讲了什么、核心内容、概括、全文”等意图后，应扩大范围并从文档不同位置抽样。
5. **删除语义明确**：
   - 删除当前文档：删除该 `document_id` 的 chunks、文档历史和相关问答，重置当前文档并刷新下拉框；
   - 清空全部文档：清空 RAG 文档和问答记录，默认保留学习笔记；
   - 清空笔记：只清理学习历史中的 notes，不应误删文档、问答或底层全部 Memory。
6. **前后端格式支持一致**：UI 可上传的扩展名必须与 `load_document()` 和 RAG 解析层一致。
7. **本地包优先**：示例入口需确保项目根目录优先于环境中的旧 `site-packages/hello_agents`，并在诊断时打印实际导入路径。

## 5. 历史开发过程

项目按以下顺序逐步形成：

1. 明确 Memory + RAG 学习助手目标；
2. 梳理 Memory、RAG、Storage、Tool 的代码归属；
3. 修复本地包导入路径；
4. 补齐 MemoryTool 抽象接口并消除循环导入；
5. 跑通 working、episodic、semantic 等 Memory 操作；
6. 跑通文本添加、检索、问答及文档导入；
7. 建立 `PDFLearningAssistant` 作为应用服务层；
8. 建立 Gradio 页面；
9. 增加多文档选择及 `document_id` 隔离；
10. 增加 PDF 页码来源和更合理的 chunk 策略；
11. 增加全文总结模式、引用格式、学习笔记、统计和报告；
12. 增加 Markdown/Word 报告导出和文档删除管理；
13. 从 PDF 扩展到 TXT、Markdown、DOCX，并统一 UI 中的“文档”命名；
14. 完善 README、演示流程和质量检查清单；
15. 规划从 JSON 向 Qdrant、Neo4j 和产品化 UI 演进。

## 6. 已遇到的典型问题

| 问题 | 根因 | 处理原则 |
|---|---|---|
| 修改本地代码却仍运行旧逻辑 | Python 导入了 Anaconda `site-packages` 版本 | 入口优先插入项目根目录，并核对 `hello_agents.__file__` |
| `MemoryTool` 不能实例化 | 未实现基类抽象方法 | 实现 `get_parameters()`、`run()` 并保持统一工具接口 |
| partially initialized module | 底层模块反向导入 Tool | 保持单向依赖，移除反向导入 |
| `NameError: List is not defined` | 类型标注缺少 typing 导入 | 明确导入 `List/Dict/Optional/Any`，或统一使用现代内置泛型 |
| PDF 来源页码不准 | 整篇读取，metadata 无页码 | 按页导入并保存 `page_number` |
| 多文档检索串库 | 缺少 `document_id` 过滤 | 所有相关链路统一传递、持久化并过滤 `document_id` |
| 全文总结不完整 | 普通 top-k 只返回局部片段 | 使用 summary mode 扩大检索并跨位置采样 |
| 清空笔记参数报错或误删 | Tool 的 forget 参数与实现不一致 | 笔记历史与底层 Memory 删除解耦 |
| UI 支持 TXT/MD，后端拒绝 | 前后端扩展名白名单不一致 | 使用同一支持格式定义并做端到端测试 |
| DOCX 无法读取 | 缺少解析分支 | 使用 `python-docx` 提取非空段落并保留来源元数据 |

## 7. 当前能力基线（需由代码和测试复核）

- 多格式文档导入：PDF、TXT、MD/Markdown、DOCX；
- 文本切块、嵌入和本地 JSON RAG 持久化；
- 当前文档隔离问答与检索；
- 来源、页码、相关度和引用格式；
- 学习笔记、记忆回忆、学习统计；
- 学习报告及 Markdown、Word 导出；
- 删除当前文档、清空全部文档和清空笔记；
- Gradio 可视化界面。
- 多文档问答已支持 1–10 篇范围选择、联合问答、对比分析、联合总结、稳定来源引用、上下文预算和有界并发 map-reduce；验证时必须使用项目 `venv`。
- 本地多用户注册、登录、会话过期与退出；用户 UUID 目录隔离；同用户多会话共享 Runtime、会话级当前文档独立；报告、迁移和损坏恢复均按用户作用域处理。

不要仅依据历史对话宣称某项已完成。开始相关工作前，应检查对应代码并运行最小验证。

## 8. 后续路线

### 学习 MVP（2026-09）

- `app/learning_repository.py`：每用户 `learning.json`，版本 1，原子更新、损坏失败关闭和按文档级联清理。
- `app/learning.py`：1–365 天确定性计划、每日 5–480 分钟、SM-2、复习日志和到期查询。
- `app/learning_generation.py`：只消费公共 `RAGTool.execute_result("search")`，校验真实来源、数量和重复内容，返回整批草稿。
- `app/runtime.py` 与 `PDFLearningAssistant` 共享用户锁和学习服务；生成期间释放锁，提交前重验文档与重复内容。
- Gradio“8. 学习中心”提供今日任务、学习计划、知识卡片、练习题；每次生成仅限一篇文档，数量为 3/5/10，简答题由用户自评。
- 学习数据不进入 Memory，也不修改既有问答统计。删除文档时清理对应学习数据，失败须报告部分失败。
- 两用户离线闭环验收：`tests/integration/test_learning_mvp_acceptance.py`，覆盖实际生成服务、来源持久化、复习、重读及删除隔离。
- 2026-09-06 验收：全量 988 passed、7 skipped；最终修复另通过 RAG 58 项及鉴权 21 项定向回归。完整范围和环境限制见 `docs/superpowers/plans/2026-09-06-learning-mvp-acceptance.md`。

### 工程升级（优先）

1. 引入 Qdrant，并保留 JSON/Qdrant 后端切换能力；
2. 为 Qdrant 实现 payload 过滤、collection 管理及 `document_id` 隔离；
3. 接入 Neo4j，逐步形成实体、概念关系、章节关系和 GraphRAG；
4. 将当前单进程多用户协调升级为可选的多进程/分布式会话与锁；
5. 支持多文档联合问答、对比和总结；
6. 增加批量导入、异步任务、进度、失败重试和任务队列。

建议配置方向：

```env
RAG_BACKEND=json
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=doc_learning_vectors
```

### 产品化

- 改进布局、文档管理、历史侧栏和加载/上传进度；
- 检索关键词及命中片段高亮、引用一键复制；
- 学习计划、间隔复习、遗忘曲线和知识掌握度；
- 自动生成知识卡片、问答卡、选择题和简答题；
- Docker/云端/Hugging Face Spaces/内网部署。

## 9. 后续开发检查清单

每次修改 RAG、文档导入或 UI 时，至少考虑：

- 是否破坏 `document_id` 隔离；
- metadata 是否仍包含来源及 PDF 页码；
- 新格式是否贯通 UI、Assistant、Tool 和解析层；
- JSON 持久化是否仍可重启恢复；
- 删除操作是否只删除目标范围；
- 总结问题和局部检索是否都正常；
- 是否实际加载本地 `hello_agents`；
- 是否验证了上传、切换、问答、检索、引用、报告和删除的受影响路径。
## 10. 多文档问答质量增强（2026-07）

当前已按推荐顺序落地：

- `evals/data/multi_document_qa.json` 与 `evals/multi_document_qa.py`：离线 golden cases 覆盖联合问答、对比、联合总结和缺失文档。
- `SimpleRAGPipeline` 与 `RAGPipeline`：`retrieval_mode="vector|hybrid"`，hybrid 融合词面召回；`use_mmr`、`mmr_lambda` 和 `vector_weight` 控制多样性与权重。默认仍是 vector。
- `RAGTool`：单篇总结使用进程内有界缓存，键包含命名空间、文档内容/版本、问题、上下文上限和提示词版本；文档替换后自动失效。
- `PDFLearningAssistant` / `app/summary_tasks.py`：联合总结支持后台任务 ID、queued/running/progress/completed/failed/cancelled 状态、进度回调和协作式取消；Gradio 提供启动、查询、取消按钮。
- 来源与对比：`RAGTool._last_action_data["sources"]` 提供 citation ID、document ID、页码、原文片段和可复制 reference；`structured_output=True` 时校验对比 JSON，校验失败保留 Markdown。

验证记录：

- `.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-roadmap-full`：493 passed, 4 skipped。
- 直接执行 `.\venv\Scripts\python.exe -m pytest -q` 时，Windows 系统临时目录无访问权限；使用仓库内 `--basetemp` 后完整套件通过。
- `.\venv\Scripts\python.exe .\ui\gradio_app.py` 可启动并保持服务运行；验证后已停止本次启动的进程。
