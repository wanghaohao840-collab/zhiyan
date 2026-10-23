# Plan Review: trusted user initialization

- Source plan: `docs/superpowers/plans/2026-10-22-trusted-user-initialization.md`
- Reviewed commit: `e24deebb2b06d0b37f3c9dc516fcc4771e4a1e21`，另有本阶段新治理文档。
- Review date: `2026-10-22`
- Verdict: `accepted`；首包已实施且获独立 SPEC/QUALITY 验收，后继包仍待交付。

## Repository evidence

- `app/postgres_auth.py:18`：register 自己提交事务，不能直接用于建户及双基线原子组合；新 cursor helper 是提议接口。
- `app/postgres_snapshots.py:106`、`app/postgres_coordination.py:77/110/196`：快照 CAS、provisional lease 获取/锁后活性/释放支持 caller cursor。
- `app/postgres_vector_generations.py:197/261/298/359`：stage/seal/publish_user 各自开事务，只有内部 _publish 已接收 cursor；新 pair 入口需保留原状态转移和 authority 检查。
- `app/postgres_history_document_witnesses.py:105/218`：read_current 和 insert 可在同 cursor 读取/写入已发布 receipt 的 witness。
- `app/isolated_import_runtime.py:37/230/305`：当前固定 head16、恰好两个 trusted ID；新配置不能制造假 ID，schema17 需明确兼容策略。
- 既有测试夹具只准备测试基线，不是生产初始化实现。计划引用的既有测试路径已核实存在；新增文件和接口均明确为提议。
- 原431项输入和15个受保护 dirty 文件原始字节均与 Task07 基线一致。15路径完整列在首包，禁止重置、归一化、改写或暂存。
- 本轮只读审查源码、JSON/XML/AST 和文件摘要，没有运行测试、迁移、初始化或服务操作。

## Findings

### Blocking

设计和计划无未闭合 blocking。首包静态 READY 条件已满足；每次测试执行仍须独立 root GO。

### Required revisions

已闭合 N1 F1–F7：取消多余 preparing/claim；合法双空发布调用图；不可变操作及完整来源凭据；已使用账户的历史只读重入；首批账户资源专用配置；16/17 显式兼容；真实单事务过期/崩溃语义。预检顺序及 busy/expired/timeout/unknown 分类冲突也已修正。

N2 R1 已闭合：Task01 回归和最终 combined 命令完整执行新增 settings 测试模块，不依赖 -k 子集覆盖。

### Non-blocking notes

首包实现及运行结果已独立验收；完整新用户初始化仍需 Task02–05 和最终整体验收。

## Accepted scope

- Goal：运维显式创建真正新用户，在同一个 PG 事务中建立可信完整空基线和完成凭据。
- In scope：资源专用 settings、加法迁移17、严格16/17门禁、同 cursor 双空基线、建户事务和 PG 历史证据重入、getpass CLI、真实并发/故障/readiness/首次导入验证。
- Out of scope：公共注册、普通 distributed bootstrap、历史用户补空、外部写入/资源创建、历史 GC/迁移/向量忠实性、main 和生产。
- Compatibility：原 API/Worker 角色、启用条件、两 ID 限制及错误语义保留；原 register/单 scope authority 行为兼容；新字节必须新验证。
- Architecture/data-isolation：missing operation 才做外部只读预检；单 PG commit；user-first/fresh DB clock；事务中无网络 I/O；只读历史证明，不清空或修复已有账户。

## Packet graph

| Packet / proposed stage | Depends on | Parallel-safe | Owned files | Outcome |
| --- | --- | --- | --- | --- |
| `01-resource-settings-schema.md`（done） | none | no | shared resources/settings、migration17、schema/settings tests、runtime seam | 严格资源配置和不可变完成凭据，129独立测试验收 |
| Planned02（未生成） | 01 done | no | vector authority、新 pair test | 同 cursor 双空基线 |
| Planned03（未生成） | 02 done | no | init service、auth cursor、新 service test | 原子建户与历史重入 |
| Planned04（未生成） | 03 done | no | ops CLI、新 CLI test | getpass 与条件外部只读预检 |
| Planned05（未生成） | 04 done | no | 新 process acceptance test | 独立进程、readiness/首次导入及最终审查 |

首包已 done；后继包须基于前序已接受的实际接口重新核对并生成。源计划不能代替自包含任务包。

## Packet readiness audit

| Packet | Goal/non-goals | Context/interfaces | Prerequisites | Change boundary | Acceptance/tests | Forbidden changes | Handoff | Ready |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 done | yes | yes：实际接口已验收 | yes：设计/packet/runner/实现/证据独审接受 | yes | yes：fresh GREEN通过 | yes | yes | yes |
| 02–05 未生成 | 仅设计层面 | 无实际前序接口 | no | 仅计划范围 | 未冻结 | 仅计划边界 | 未生成 | no |

每列满足且 Astra 全文 READY 接受前，不得将任务包标 ready 或实施。

## Integration verification

首包使用固定 venv、唯一 RED/GREEN 命名空间及经审 runner/root GO，完整新增配置/schema测试与规定回归通过。unit32/schema37/regression129、129独立节点、198次执行、594条phase通过，零FES；后继包继续按相同证据原则冻结各自范围。

必须覆盖：schema16/17/future18；ledger 不可变/digest/FK；同 cursor 回滚；同 operation/username 的实际进程竞争；自然租约过期及超时分类；提交 unknown；导入后重入零写；无外部写；显式加载两个新 ID 后原 readiness。不得机械重跑或累计旧1020/75数量。

## Final integration review requirement

所有实施包 done 后，必须创建 `docs/agent-workflow/task-packets/2026-10-22-trusted-user-initialization/FINAL_INTEGRATION_REVIEW.md`。采用完整仓库模板，核对实际组合差异、接口、原子性/隔离、实际故障证据和独立 SPEC/QUALITY；结论仅 accepted / changes-required / blocked。main、生产和父任务完整升级仍是独立门槛。

## Open decisions

首包无未解决阻塞。下一项是基于实际 authority 的同 cursor 双空发布；后继服务/CLI/真实进程及最终集成尚待完成。

### 2026-10-23 测试范围现实修正

原 runtime 模块的 `real_isolated_resources` 同时需要 PG/S3/Qdrant。Task01 的 PG-only 回归改为完整新增 settings/schema 两模块、完整 `test_postgres_schema.py`，加原 runtime 的 11 个非 real 函数显式 selector；参数化节点由独立 collect 展开，不用全局 `-k` 过滤新增模块。全部选定必需用例零 skip，原 runtime `test_real_*` 留 Task05 多服务验收。此处是待首包及 runner 精确冻结后独审的新执行范围，不能冒称原 Astra 设计合审已接受这些新字节。

## Independent evidence and documentary boundary

### 首包实现与运行验收

最终 Astra SPEC/QUALITY 报告 `init-t1-accepted-astra-n1.md` SHA256 `53615246d48210df450847f9ce44938fc6c398527fda09c765d175b76de5f791` 接受当前首包六个变更源文件与 fresh n4 证据。root 组合记录 `df2d42df645ad36b63f98a6f3484b711e10fc60e8ab01d816e076f72761845cf`，三执行阶段同 source-map `aa78f654b264dfb8c9c0f2ab8090facc965d046854caa1e1f3c3555f429c89c6`。旧15dirty raw保持原样；测试schema清理完成。此处和packet done/handoff为验收后文档更新，READY受审/执行字节另存，不冒充未变化的原packet SHA。PG17.6 canonical比较已实证，其他PG版本不由本包推定兼容。仅首包完成，未初始化生产用户或交付后继服务。

### 2026-10-23 首包静态 READY 接受

Astra 报告 `init-t1-ready-astra-n1.md` SHA256 `f8579c86ed040b304ede12d43fd82217c418ec5887cb21d68b5c7924a49b3478` 最终 SPEC/QUALITY 接受 packet `44cb73d4520b7c35492dc73795cf069f5c73d28c267e072f16d191987f0e659f` 和 runner `a1ac9a7a7fa3188d0a1d00070a596b275633be96f047ba2094486d1be0910d22`。唯一 R1（旧 direct/env 错误优先级及 pool 提前校验）已由 raw kwargs parser、identity/pool 分离和差分测试要求闭合。root 全文读报并核 SHA，仅将 packet 状态改 ready；后续 GO 绑定新状态字节。首阶段仅授权 Sol 创建两份新测试，三份新产品文件仍须不存在，真实 RED 由 root 执行后才授权产品实现。无测试、迁移或服务操作。

Astra N2 `init-design-plan-astra-n2.md` SHA256 `27ce855cce6b9cbc8467bcc9036896739751c6b84b7dfc9282c74496846aa722` 接受 plan 原件 `525de02e…` 与 spec 原件 `1e450712…`；原审副本保存在忽略的 runtime。Sol 现实核对报告 SHA256 `d1405a896e56d75de110358b8f5ce044550135e57767b6c1ae9d067170e66f5a`。root 全文读报并检查当前代码/计划；后续状态段是文档记账，不冒称原受审字节，也不构成实现验收。
