# 单节点 Docker 部署

本目录用于在一台 Linux 云主机或内网服务器上运行单副本文档学习助手。默认
启动 FastAPI/React 应用与 Qdrant；Neo4j 通过 `graph` Profile 按需启动。当前
Session 和用户锁位于进程内，因此必须保持一个应用容器、一个 worker。

## Windows operations

For the supported Windows single-node operator workflow, including the Private
network preflight, scheduled backups, restore drills, upgrades, and uninstall
boundaries, see [deploy/windows/README.md](windows/README.md). The Windows
operations defaults are documented in `.env.example`; use explicit script
parameters for an operator-selected state or backup root.

## 前置条件

- Linux 主机；
- Docker Engine 和 Docker Compose v2；
- 可访问的外部或内网 OpenAI 兼容 LLM 接口；
- Qdrant 数据必须位于本地或块存储支持的 POSIX 文件系统，不能使用 NFS。

## 首次启动

```sh
cp deploy/.env.example deploy/.env
```

编辑 `deploy/.env`，至少替换：

```dotenv
LLM_API_KEY=replace-with-your-key
LLM_BASE_URL=https://your-llm-endpoint.example/v1
LLM_MODEL_ID=your-model
```

`LLM_BASE_URL` 可以是公司内网的 OpenAI 兼容网关，也可以是服务器可访问的
外部接口。把 `APP_UID` 和 `APP_GID` 改为运行 Docker Compose 的部署账号：

```sh
id -u
id -g
```

不要提交 `deploy/.env`。

使用同一个部署账号创建宿主机数据目录并启动。Qdrant 使用
`QDRANT_VOLUME_NAME` 指定的 Docker 命名卷，数据位于 Docker 主机的 POSIX
存储中：

```sh
mkdir -p deploy-data/app
docker volume create --label com.zhiyan.role=qdrant-data zhiyan_qdrant_data
docker compose --env-file deploy/.env up -d --build
docker compose --env-file deploy/.env ps
python3 deploy/smoke_test.py --env-file deploy/.env
```

默认只监听 `http://127.0.0.1:7860`。需要可信局域网访问时，显式设置
`APP_BIND_ADDRESS=0.0.0.0`，并用主机防火墙仅允许 Private/LocalSubnet 或
等价可信网段。公网不得直接暴露应用端口，必须在 Compose 之外增加 HTTPS
反向代理或网关、证书和访问控制。

## 启用 Neo4j

先在 `deploy/.env` 设置：

```dotenv
ZHIYAN_GRAPH_ENABLED=1
NEO4J_URI=neo4j://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=使用新的强密码
NEO4J_DATABASE=neo4j
```

然后启动 Profile：

```sh
mkdir -p deploy-data/neo4j/data
docker compose --env-file deploy/.env --profile graph up -d
docker compose --env-file deploy/.env --profile graph ps
```

Neo4j 不参与当前主问答召回。图谱失败不会撤销已经成功的 RAG 导入。
关闭 `ZHIYAN_GRAPH_ENABLED` 会停止查询和构建新图谱。此前创建过图谱的文档在删除时仍需清理 Neo4j 数据。Neo4j 离线时，文档立即隐藏，清理任务保留并可重试。

## 冒烟检查

默认检查不调用 LLM，也不创建业务用户：

```sh
python3 deploy/smoke_test.py --env-file deploy/.env
```

上线验收时执行深度检查。它会在临时目录和唯一 Qdrant namespace 中导入
测试 TXT、检索来源并调用一次 LLM，结束后清理测试数据：

```sh
python3 deploy/smoke_test.py --env-file deploy/.env --deep
```

## 候选文档嵌入配置与探测

候选模型使用 `BAAI/bge-m3`，不要选 `Pro/`。在本地 `deploy/.env` 的
`RAG_EMBEDDING_API_KEY=` 后填写密钥，不替换任何 `LLM_*`。
保持 `RAG_EMBEDDING_PROVIDER=simple`，直到版本化索引重建和切换验收完成。

从仓库根目录仅校验配置，不联网：

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m deploy.embedding_probe --env-file deploy/.env
```

明确发送内置公开句子进行候选模型连通性测试：

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m deploy.embedding_probe --env-file deploy/.env --probe
```

Linux 环境将解释器路径替换为项目环境的 `python`，仍从仓库根目录执行。
探测输出不包含密钥、全文向量或业务文档。密钥只在本地填写，不发送到聊天。
API 费用以平台当前政策为准，免费服务也有限流。探测通过只证明候选配置
及请求协议可用，不代表文档检索已切换，不代替检索质量评测或 deep smoke。

客户端使用直接 HTTPS 连接，不继承系统代理。若网络必须使用企业代理，
应先评审代理配置与密钥传输边界，不能关闭 TLS 校验绕过连接失败。

## 日常操作

查看状态和日志：

```sh
docker compose --env-file deploy/.env ps
docker compose --env-file deploy/.env logs --tail 200 app qdrant
```

重启或关闭容器不会删除宿主机数据：

```sh
docker compose --env-file deploy/.env restart
docker compose --env-file deploy/.env down
```

不要把 `docker compose down --volumes` 作为常规操作。

## 冷备份

备份会短暂停止当前运行的应用、Qdrant 和已启用的 Neo4j，以确保 SQLite、
JSON 和数据库文件一致。脚本会在退出时重新启动原先运行的服务。
备份与恢复都必须使用启动 Compose 的同一个部署账号执行，不要使用 `sudo`，
从而让恢复后的应用文件继续归 `APP_UID`/`APP_GID` 所代表的账号所有。

```sh
sh deploy/backup.sh --env-file deploy/.env
```

默认输出到 `backups/`，包含归档、SHA-256 和不含密钥的元数据文件。
`deploy/.env` 不会进入备份。可以改变备份目录：

```sh
sh deploy/backup.sh \
  --env-file deploy/.env \
  --backup-root /srv/document-assistant-backups
```

备份目录必须位于 `DEPLOY_DATA_ROOT` 之外。

## 恢复

恢复会验证 SHA-256、拒绝路径穿越与链接成员、停止服务，并把当前数据目录
重命名为带时间戳的 rollback 目录。若新数据无法健康启动，脚本会自动换回
原目录；失败的恢复数据和 staging 目录会保留用于排查。

```sh
sh deploy/restore.sh \
  backups/assistant-20260730T120000Z.tar.gz \
  --env-file deploy/.env
```

恢复成功后先登录、检索并检查报告，再手动清理 rollback 目录。脚本不会递归
删除旧数据。

## 数据与秘密边界

- `deploy-data/app`：SQLite、用户文档、History、Memory 和报告；
- `QDRANT_VOLUME_NAME`（默认 `zhiyan_qdrant_data`）：POSIX 命名卷中的向量数据；
- `deploy-data/neo4j/data`：可选图谱数据；
- `deploy/.env`：LLM Key 与 Neo4j 密码，仅保存在部署主机；
- `backups/`：数据归档和校验文件，不包含 `deploy/.env`。

Qdrant 和 Neo4j 没有宿主机端口映射。应用端口仍应由防火墙限制在可信内网。

## Windows Docker Desktop

Windows 必须使用 Docker Desktop 的 Linux/WSL2 后端，并将 Qdrant 保存在命名
卷中，不能把 `/qdrant/storage` 绑定到 NTFS。登录恢复、五分钟巡检、每日冷备、
每月隔离恢复演练、安全更新，以及从旧 NTFS 目录迁移的完整步骤见
[`windows/README.md`](windows/README.md)。正式计划任务只能从稳定部署目录安装，
不能长期指向临时 Git worktree。
