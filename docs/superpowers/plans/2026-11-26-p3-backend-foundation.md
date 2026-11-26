# P3 后端兜底实施计划

在单个 Uvicorn worker 下交付问答事件流、文档定位和受限图谱查询。SQLite 继续保存最终问答结果。读取与后台提交复用当前用户归属检查和删除栅栏。

## 1. 检索与问答

- [x] 合并清单指定的检索质量提交 `f34d7b4`。本地 `main` 的合并提交为 `3b0d231`。摘要试点沿用已有默认关闭的开关，后续提交留在摘要分支。
- [x] 给提问接口增加 `?stream=true`。现有问答线程从有界队列取出请求，用实际执行情况上报阶段。普通联合问答传输模型增量，结构化回答和证据核验结果完成并保存后发送正文。
- [x] 使用会话 Cookie 和 SQLite 删除栅栏。每 15 秒保活，断流时从消息接口恢复。重排和证据核验按已有配置执行，阶段轨只记录实际发生的阶段。

## 2. 文档定位与原文

- [x] 在问答引用和检索定位中保留片段标识。新索引保留最多前后各 350 个 Unicode 字符的原文上下文。旧索引返回摘录和空偏移。
- [x] 接入受鉴权的 PDF、Markdown 和 TXT 原始字节与单段 Range 读取，覆盖旧文档编号、同用户归属和删除后的 404。

## 3. 知识图谱

- [x] 通过显式开关和 Neo4j 连通性提供能力探测。子图使用用户命名空间并在返回前复查文档可见性，节点和关系各最多 200 个。
- [x] 在 Neo4j 写入前持久化清理责任。即使关闭图谱开关，删除此前建图的文档也会重试清理。冷备和恢复校验 SQLite、Qdrant 和 Neo4j 的同批归档，恢复补偿失败时保持服务停止。
- [x] 使用真实 `neo4j:5.26.28-community` 容器跑通 `tests/integration/test_neo4j_live.py`（隔离、多标签/类型安全 Cypher、节点与孤立 `CanonicalEntity` 级联删除）及真实 Neo4j 冷备恢复演练；同步完成 Linux `deploy/backup.sh` / `deploy/restore.sh` 停机与双归档校验；导入建图重构为 `GraphProjectionWorker` 异步可重试队列、`GraphService` 改为托管长连接池 + `/capabilities` 5s TTL 缓存。

## 2026-11-26 复审收口记录

- **P3-1**：归一化 `HelloAgentsLLM.stream_invoke(..., strict=True)` 的 `finish_reason`（忽略空字符串 `""`，允许 `"stop"` / `"length"`，实机通过 `deepseek-v4-pro` 流式冒烟 27 chunks）；SSE 接入 `request.is_disconnected()` 与 `QaLiveAnswer` 跨线程事件唤醒，并在 `completed` 分支补读竞态尾帧；队列满前原子预占位拒绝返 `503 QA_ENGINE_BUSY` 防污染历史；`POST /messages/{id}/retry` 接入 `?stream=true`；修复 `QaPage` 切会话重复建连旧消息与 `QaWorkspace` 阶段轨视觉基线兼容。
- **P3-2**：非 PDF 原文流声明 `text/plain; charset=utf-8`；`DELETE /documents/{id}` 与预览对称支持标准 UUID 及 16–64 位十六进制旧格式文档 ID（继续拒绝任意非法字符串返回 422）；DOCX 等不支持字节流的格式对归属用户返回 `415 document_content_unsupported`（跨用户仍返回 `404`）；新增前端 Unicode code point 切片工具 `web/src/features/documents/preview.ts`。
- **P3-3 & 分支收口**：合并 `codex/rag-retrieval-quality`（`78aeedf`）全量 10 个后续提交；重构 `GraphProjectionWorker` 解耦主导入；升级 `neo4j_store.py` Cypher 多标签与 `toString()` 防御；托管 Neo4j Driver 长连接池与 `ApplicationServices.stop()` 关闭；同步 Linux `backup.sh` / `restore.sh`；真实 Neo4j 5.26.28 容器 `2 passed` + 真实冷备恢复演练 `cnt=1`；`codex/zhiyan-summary-workflow` 保持为独立默认关闭的摘要试点分支。

## 2026-11-26 验证记录

- 后端 22 个相关测试文件共 318 项通过、3 项跳过。其中两项跳过是未配置真实 Neo4j 的集成测试。加入旧格式文档编号支持后，再跑预览、图谱和备份聚焦测试，11 项通过。
- 前端使用单测试进程运行，28 个文件的 222 项测试全部通过。类型检查、问答相关 ESLint 检查和生产模式构建通过。用隔离的 FastAPI 进程和 Chromium 在桌面、平板、手机验证了实时阶段、增量文字和刷新后恢复。手机发送按钮调整后单独重跑通过。
- 完整后端首轮 2475 项通过、9 项跳过、6 项失败。两项引用评测问题已修复并通过复跑。记忆文件替换和 Gradio 模块导入超时单独复跑通过。Windows 长短路径比较和旧问答样式断言在原分支也会失败。
- 远程主分支与运行中的服务未变更。生产网关缓冲行为和真实模型流式输出仍需在发布环境验证。
