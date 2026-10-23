# 受信新用户初始化设计

- 日期：2026-10-22，Asia/Shanghai。
- 状态：用户“继续执行”采纳推荐范围；首包资源配置/schema17已实施并于2026-10-23获独立验收。双空发布、建户服务、CLI和最终真实进程验收仍待后继包，未运行生产初始化。
- 基点：codex/distributed-cutover-vertical-slice@e24deebb2b06d0b37f3c9dc516fcc4771e4a1e21。
- 父设计：2026-10-10-isolated-import-runtime-design.md第2/8节；Task07已验收的是具备可证明基线的隔离导入，不包含生产新用户初始化。

## 1. 目标和非目标

增加仅供运维显式调用的初始化入口：本操作创建真正新账户，并原子持久化完整可信空History/Memory与RAG/episode双基线。重复调用返回相同初始化结果；提交未知时按预先保存的operation_id查证；不得把缺失、损坏或未迁移历史账户当成空账户。

不增加公共注册路由、不启用普通distributed bootstrap、不自动修改trusted名单、不迁移保留数据、不触及源Qdrant、不增加业务接口或历史GC、不合并main、不发布生产。历史迁移仍保留文本/元数据，向量回忆待补证。

## 2. 实际接口与设计依据

| 当前源码 | 已核事实 / 实施约束 |
| --- | --- |
| app/postgres_auth.py:18 | register自行开事务；不能先调用它再声称账户和基线原子。复用auth校验、scrypt hash和verify_password，不能存明文或比较每次随机盐hash是否相等 |
| app/postgres_snapshots.py:106 | compare_and_swap_in_transaction可在同cursor首次写History/Memory version1；expected_version必须0 |
| app/postgres_coordination.py:77/110/196 | acquire_in_transaction、require_live_in_transaction、release_in_transaction可组成同事务provisional lease；READ COMMITTED、user-first和锁后DB时钟保留 |
| app/postgres_vector_generations.py:197/261/359 | stage、seal、publish_user各自开事务；不能两次publish_complete。_publish:298是可复用的内部cursor单scope转移，新增窄pair入口须同cursor原子组合 |
| app/postgres_vector_generations.py:335 | 空head的generation_id为NULL，但last_generation_id必须指向expected_count0的真实published receipt；missing绝不等于empty |
| app/postgres_history_document_witnesses.py:105/218 | read_current支持cursor；insert支持同事务witness，且即时trigger要求RAG receipt先published |
| app/isolated_import_runtime.py:37/305/581 | 当前仅精确head16；readiness需完整schema/profile、双receipt、History witness、Memory/episode投影及前后权威匹配；trusted IDs恰好两个 |
| hello_agents/memory/storage/vector_store.py:429 | require_collection只读核存在/dimension/distance，不能用ensure_collection悄然创建目标 |

Sol现实核对原件init-design-sol-n1.md（SHA256 d1405a896e56d75de110358b8f5ce044550135e57767b6c1ae9d067170e66f5a）在忽略目录，记录详细签名和约束；这是源码审阅，不是测试结果。

## 3. 方案选择

| 方案 | 评价 |
| --- | --- |
| 采用：外部只读预检 + 单PG事务建户及双空基线 | 空用户无需S3对象或Qdrant点，原子边界完全落在PG；最小可靠方案 |
| 原提案两阶段preparing/claim/恢复 | 只有需要创建外部资源或预装非空数据才需要；当前会引入多余持久状态和恢复责任，取消 |
| 既有账户补空 / 同期开公共注册 | 无法由空表证明新账户来源或会扩大产品安全范围，本阶段不采用 |

本次细化取消原草案的preparing/跨进程初始化claim，语义仍覆盖原子、并发和崩溃；并非将这些要求删除。未来涉及外部写入必须重新审查，不能沿用单事务结论。

## 4. 输入、配置与输出

CLI使用新资源专用TrustedUserInitializationSettings，不包含role或trusted_user_ids，也不要求首批新户预先存在。将现有runtime资源字段解析和验证提取为共享私有helpers供原settings和新settings调用；保留schema/普通资源/保留target隔离、完整profile、凭据隐藏及pool边界。原IsolatedImportSettings仍原样要求api/worker角色、ISOLATED_IMPORT_ENABLED及恰好两ID，新初始化另需TRUSTED_USER_INITIALIZATION_ENABLED=1。新设置沿用ISOLATED_IMPORT_*资源字段，不接受dummy IDs、不扩大名单。CLI独立打开受限客户端，绝不启动API、runtime或Worker。新增迁移仅在显式一次性目标schema实施；初始化程序不自动upgrade。

输入：调用前由运维保存的canonical UUID operation_id、username、通过getpass输入并确认的password。禁止password argv、env新明文机制、日志和状态表。新user_id由服务随机生成，入口不接受历史user_id。username规范化和password验证/哈希在事务外完成；哈希在repr和日志中隐藏。所有错误用code，不回显SQL、DSN、密码或原文。

输出：operation_id、user_id、durable_initialized=true和replayed。此结果仅证明初始化已提交，不宣称当前ordinary_ready。原运行时仍要求该ID显式列于恰好两个trusted UUID中、schema/provider/storage健康和完整读回；由运维独立配置，不由本命令修改。失败时不输出成功，提交未知则输出可安全识别的unknown状态并保留operation_id。

## 5. 完成凭据与schema兼容

新增加法migration20261022_17（down_revision=20261007_16），仅增加trusted_user_initializations完成凭据和其约束/trigger，不批量创建历史基线。凭据没有preparing/claim状态，只能与新账户和完整双基线同事务出现。

表必须固定：operation_id UUID主键、format_version整数且固定1、user_id TEXT唯一且引用users、原username_key、完整非密资源描述（实际database/schema、带协议host/port的服务端点、bucket、两collection和完整profiles，排除用户名/密码/query等凭据）及其规范摘要、初始History和Memory规范JSON SHA256、两个完整IndexIdentity JSONB、rag namespace及episode namespace、两初始generation UUID（外键约束对应tenant/kind/namespace/index_key）、初始revision/index_revision/snapshot_version均1、空manifest摘要、初始History证据摘要、completed_at DB时间。完整identity/namespace及原操作绑定不可变，禁止UPDATE/DELETE；不保存密码或session。凭据resource descriptor及初始snapshot摘要统一使用PostgreSQL内置encode(sha256(convert_to(jsonb_value::text,'UTF8')),'hex')，即同一服务器JSONB::text规范表示；存储和重入计算使用同一表达式并比对完整JSONB语义值，不能混用Python无空格JSON摘要，不引入pgcrypto。History witness仍使用原document_evidence算法，vector空manifest仍使用原SHA256([])，两者不能混淆。降级遇非空完成凭据必须拒绝，不能删除其追溯链；只允许空表的新资源降级验证。

runtime schema门禁显式接受已审16和17；16保持原relation清单，17额外要求新表及其预期schema契约，initializer只接受17，未知futurehead拒绝。不能改成任意>=16。此修改改变Task07输入，必须做受影响新回归，不能声称旧75或1020在新字节下已重新验收。

## 6. 原子调用图与来源证明

先做资源配置静态校验和实际数据库/schema/head校验，然后以独立只读事务查operation_id。已有完成记录直接走第7节历史证据验证，不访问S3/Qdrant；不存在才只读检查S3版本化bucket、两共享Qdrant collection及完整配置身份，然后进入写事务再次检查operation，防止预检期间另一进程完成。不创建外部资源、写点或对象、不调用嵌入引擎。外部故障只使新建路径在SQL写入前拒绝，不阻断已提交结果的PG查证。只读检查是时间点结果，不是持久存储健康保证。

拟新增初始化服务以及窄empty-pair caller-cursor入口（实际签名由实施计划固定，以下均是新契约，不是现有公开API）：

1. 在一个READ COMMITTED事务内设置SET LOCAL lock_timeout=5s、statement_timeout=10s、idle_in_transaction_session_timeout=15s并查operation_id。已完成则退出当前READ COMMITTED事务，再独立进入第7节一致只读查证，不插入账户，不能中途将该事务切到REPEATABLE READ。
2. 对新路径，只能INSERT本次随机user_id的新users行，不能UPSERT更新已有用户或通过缺快照推断新户。用户与operation_id的唯一冲突由PG决定赢家。完成凭据只在双基线齐备后插入，不引入可更新的reservation行；若该唯一检查等待消耗lease，5秒lock_timeout及最终新鲜lease检查决定回滚，绝不续租掩盖过期。所有冲突必须回滚整个事务，不能在已失败事务继续查询或修复。
3. 锁新user，调用原acquire_in_transaction取得60秒同事务provisional user lease，沿用owner/token/version/DBclock及无gate检查。没有独立续租线程，lease只为复用原向量authority权限；逐语句及idle上限不冒称整事务hard cap，累计预算仍由lease最终DBclock检查决定；事务任意阶段到期即回滚全部新数据。
4. 同cursor CAS写EMPTY_HISTORY和{user_id,memories:[]}，expected_version0得到v1；检查无document_objects、memory_documents和import/task/gate等既有域状态。
5. 窄pair入口验证两个精确scope、同user、真实user lease、无gate、index/head不存在和空快照。各scope分配新generation UUID，遵循staging→sealed(count0,digest SHA256([]))→_publish(revision1,index_revision1,snapshot_version1)，不允许任意points、callback、ImportAttempt或独立提交。stage/seal逻辑需从现有authority受控复用，不能复制历史夹具revision专用SQL或绕过trigger。RAG发布后read_current(cursor=...)，insert空History witness。Memory无文档行即完整空投影。
6. 写不可变完成凭据、校验同cursor双receipt/head/snapshots/witness全部匹配，末尾fresh require_live和无gate，再release_in_transaction后提交。释放或deferred约束失败必须使全部事务回滚。不得在事务中做网络I/O。

完整调用图：CLI → 配置/凭据校验 → PG只读operation查证（已有直接历史读回） → 不存在才哈希/外部只读预检 → 单PG事务{新users → provisionallease → 双快照 → 窄双空基线发布 → witness → 完成凭据 → final live/gate检查 → release} → commit → 按operation只读证据读回。

## 7. 重入、并发与不确定提交

重入以operation_id查不可变凭据，绑定原规范username和完整双identity/scope，并验证现账户口令（verify_password）及可接受账户状态；不能用session或客户端user_id代替。账户已改密码则旧口令重入拒绝，不把认证失败当作未初始化，更不重置口令。证据读回用单条scope精确JOIN或一个REPEATABLE READ READ ONLY视图核operation format/resource descriptor、user与初始generation/receipt/witness的不可变字段和空manifest，以及ledger冻结的初始History/Memory版本与规范digest；当前可变快照不能冒充保留的初始快照；初始generation可已被后续正常发布置retired，当前head和快照可以正常前进。重入零写、不获取新lease、不清空现有业务数据。

同operation/同请求两个执行者最多一个成功INSERT凭据；输家整事务回滚后仅允许一次新只读事务查赢家，验证完全匹配才返回replayed。不同operation相同username、同operation不同username/profile/口令均拒绝，不占用或覆盖赢家。仅明确的users username唯一约束或operation_id主键冲突可触发这一单次查证，不将任意SQL/网络错误当重入成功。lock_timeout（55P03）在确认回滚后返回busy，lease loss返回expired，statement_timeout返回timeout；这些分支本次不自动查证赢家或重试，运维可稍后用原operation_id明确重入。只有提交响应无法判定或无法证实回滚才返回unknown。不能有无界自动重试。

commit响应丢失时查已提交操作及完整历史证据。查到一致才返回durable_initialized，查不到或服务不可达保留unknown，绝不创建新operation或猜测失败/成功。后续运维可明确以原operation重试；已提交的旧执行者没有新capability，迟到重入也不能覆盖结果。事务前或中途崩溃应无账户/半基线可见，提交后崩溃只能有完整结果；不需要持久preparing恢复机制。

只读外部服务在commit后失效只影响当前readiness，不得因此重建已存在账户。完成凭据损坏/缺证据按corrupt拒绝修复。本阶段无历史GC或用户删除协议扩展。

## 8. 测试与交付门槛

- 单元：校验、秘密不落argv/log/repr、错误分类、无外部写入调用、零写重入和有限冲突查证。
- 真实PG：迁移17/immutability/完成凭据完整关系、每个SQL边界失败及deferred触发器失败均全回滚，唯一竞争确实等待、锁后lease到期拒绝、同事务无半基线可见。
- PG/S3/目标Qdrant：两个真新用户初始化后，显式装载两新IDs到原配置，原ordinary_ready及首次真实导入/已发布读取通过；账户正常使用后重入不清空。集合/profile/bucket不符新路径零写；源Q仅metadata。
- 独立进程：同operation和同username竞争、提交前进程死亡、提交后响应前死亡，以及替代进程读同operation；保留actualPID/creation/image/exit/源字节与每次唯一资源。
- 兼容：16原入口正常、17新入口正常、未知18拒绝；原认证、向量authority、快照、facade/Worker/API受影响测试按新迁移跑；旧Task07证据保持历史，不自动累计或全部重跑。

Sol High实施，Astra High独审迁移/并发/一致性和实际证据。固定D:\python_self_agent\venv\Scripts\python.exe、单pytest通道、一次性新schema/bucket/collection、保护15原dirty路径。完整计划、独立审查与自包含packet ready之前不写产品实现。当前无测试或服务操作，不代表此新阶段已验收。

## 9. 本轮计划文件边界

拟新增app/isolated_resource_settings.py、app/trusted_user_initialization_settings.py、app/trusted_user_initialization.py、scripts/initialize_trusted_user.py、migrations/versions/20261022_17_trusted_user_initializations.py及专用unit/integration/process测试；拟受限修改app/isolated_import_runtime.py共享资源校验和16/17门禁、app/postgres_auth.py caller-cursor新户创建、app/postgres_vector_generations.py原子双空基线cursor入口。不修改15个既有dirty路径，不导入tests夹具到产品。具体自包含任务包须锁定这些新增接口、约束和任务依赖，未锁定不得READY。

## 10. 独立设计审查接受 — 2026-10-22

Astra合审报告init-design-plan-astra-n2.md SHA256 27ce855cce6b9cbc8467bcc9036896739751c6b84b7dfc9282c74496846aa722，正式接受设计/计划。审阅spec原SHA1e4507122c5271a76b59f76259913929b6afa944a81f1462db01e83af58c9c5f及plan原SHA525de02ea98d0a3c12776e62e299889fcdaa60599865fe935c4e3b43649a8762分别保存为ignored init-spec-reviewed-root-n2.md / init-plan-reviewed-root-n2.md。此状态段仅是审查记账，核心设计未变，不把它冒称原审字节。首任务包仍draft；未冻结DDL/接口/runner不能实施。
