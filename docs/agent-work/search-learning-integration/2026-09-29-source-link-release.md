# 笔记来源回链发布记录

- 发布分支：`codex/batch-import-async-tasks`；功能提交：`a6cc8f1`，仅从 `0474ba6` 移植五个前端文件。
- 前端产物由该分支的锁定依赖构建，镜像 `sha256:7724eef7a9736333fe90f843336925a74a743d7fd11e9d92fdd63437106b3d57` 于 2026-09-29 以 `Publish-Frontend.ps1` 发布。发布前镜像 `sha256:ae5da95c12d8a56280d9da2a60ca300515c89fcdcd2dba2f8c36fb7ef2d49938` 保留为 `zhiyan-app:rollback-before-source-link-a6cc8f1`。
- 发布脚本在切换前生成 SQLite/文件数据与 Qdrant 的配对冷备份，记录在 `deploy-state/ui-release-a6cc8f1/backup.json`；`rollback.yaml` 保留发布前配置。上述运行资料及账号验收截图只留本机，不提交仓库。
- 相同前端源码此前通过 206 项单元测试、20 项相关后端测试、三个视口的检索端到端测试，以及构建和 lint；发布工作树重新构建成功。新镜像的后端文件层与旧镜像一致。
- 2026-10-28 重新启动 Docker 后，应用与 Qdrant 均健康，容器运行的镜像 ID 与发布记录相同；当前 Compose 配置与候选配置的哈希一致。HTTP 上四个静态文件的哈希与构建产物一致，`/notes` 与 `/search` 路由正常；未登录访问笔记和文档私有 API 返回 401。配对备份校验和、备份及运行库的 SQLite `quick_check` 均通过。细项见本机 `deploy-state/ui-release-a6cc8f1/verification.json`。
- 真实账号登录后打开一条已有文献来源的笔记，来源卡片出现“检索该文档”；点击后进入带目标 `document_id` 的 `/search`，实际目标文档被选中（研究范围 1/10），查询输入为空，页面停留在未提交的检索状态。截图保留在本机发布目录。

本次只发布前端层，未执行数据库迁移。上述账号验收覆盖来源回链；先前的保存笔记全流程由该变更的隔离端到端测试覆盖，未在这次复验中再次创建生产笔记。
