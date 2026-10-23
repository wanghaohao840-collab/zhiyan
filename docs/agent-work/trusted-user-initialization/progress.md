# 受信新用户初始化进度

## 2026-10-22：设计与计划细化，尚未实施

基点e24deebb2b06d0b37f3c9dc516fcc4771e4a1e21，Task07隔离导入已验收且远端核同。本阶段承接既有批准顺序，只做受信新用户初始化；用户“继续执行”采纳运维显式入口、公共注册关闭、不补空历史账户的推荐范围。未运行服务、迁移、初始化或测试，未修改产品源码。

Sol High只读现实核对报告init-design-sol-n1.md支持外部只读预检+单PG事务。Astra High首轮报告init-design-astra-n1.md要求闭合F1–F7：原两阶段preparing/claim取消；现有stage/seal/publish_user不能组合；完成凭据不可变并绑定完整资源/profile/初始摘要；重入核历史published/retired receipt而不清空现数据；新账户初始化完成与现allowlist/readiness分开；runtime需明确16/17兼容且initializer只17；真实lease/timeout/崩溃语义须与单事务一致。

root已修设计、Sol完成五串行任务计划：1资源配置/完成凭据/schema兼容；2同cursor双空基线；3新账户事务与重入证据；4运维CLI；5真实进程/原readiness与首次导入验收。初次账户无需预有两trusted IDs，使用资源专用settings和共享纯验证；原API/Worker恰好两人限制不改。oplookup在外部预检前，已完成结果不受当前S3/Q故障阻断。已知unique冲突最多一次新只读查证；确认回滚的locktimeout/lease loss/statementtimeout分别busy/expired/timeout，本次不自动重试。无法确认提交或回滚才unknown。

当前设计/计划待Astra最终合审；无任务包READY、无实现GO。迁移列/约束与新增接口将在自包含任务包冻结；后继包必须基于前序已接受实际源码再核对。原431输入和15保护raw字节已核未变，旧Task07证据不冒充未来新runtime字节结果。当前新文档未提交；不合并main、不发布生产，保留数据和向量provenance边界不变。

## 2026-10-22：设计/计划独审接受，首包草稿已创建

Astra N2最终报告27ce855cce6b9cbc8467bcc9036896739751c6b84b7dfc9282c74496846aa722接受设计1e450712…和计划525de02e…；F1–F7及R1均闭合，root全文读报。Sol最终仅缺新增完整settings测试命令2行，因代理pending_init由root接管机械修正；未改其他计划行。受审原件另存runtime，后续文档状态记账与原审字节区分。

已创建REVIEW和01-resource-settings-schema.md草稿，明确首包DDL/约束/catalog/helpers/runner尚待精确冻结，未标READY、未实施。后继4包仅计划，待前序实际接口独立核对后再生成，不以隐藏会话代替自包含契约。原431/15raw未变，无运行测试或服务。设计阶段已完成，初始化功能尚未实现。

## 2026-10-23：恢复首包冻结及测试范围核对

root 实查 HEAD 仍 e24deebb，原 431 输入（429 tracked + 2 ignored helpers）和 15 保护文件 raw 字节均未变；无 pytest/run-init/run-p7 遗留 Python 通道。Docker server 29.6.2 可达，精确测试 PG 与源 Qdrant 均 exited，卷身份吻合，未启动服务。第一项任务包及私有 runner 半成品已恢复由 Sol High 完成冻结，等待 Astra High 独审；未写产品或测试，未运行迁移或 pytest。

发现原 runtime 模块混含 PG/S3/Qdrant real 测试。首包 PG-only 回归将完整执行新增两个模块和旧 PG schema 模块，并显式选择原 runtime 的 11 个非 real 测试函数，独立 collect 展开参数化节点，必需用例零 skip。原 runtime real 测试留 Task05 多服务验收；plan/REVIEW 已记录现实修正，精确 packet/runner 待独审，未标 READY。

Sol 静态冻结首包 `4b613015be586b4f34efddda91452c2dc820aadca51286de1a2bd72e512588ec`、runner `a1ac9a7a7fa3188d0a1d00070a596b275633be96f047ba2094486d1be0910d22`，交接 `init-t1-freeze-sol-n1.md`。root 全文读取并核 SHA，另存原字节 `init-t1-packet-reviewed-root-n1.md` / `init-t1-runner-reviewed-root-n1.py`。已交 Astra High 独审迁移/门禁/配置兼容与运行器证据，尚无结论，packet 保持 draft；无 test-only 或产品实现 GO。恢复核验记录 `init-t1-resume-root-n1.json` SHA256 `d9a2ffe1c3000e7d6e5b24e11b210e5f1c1f3eaee14845cc3195e8fcc41e1796`。

Astra N1 发现并复审闭合 R1：原 direct/env 配置错误优先级与 pool 提前校验。最终报告 SHA256 `f8579c86ed040b304ede12d43fd82217c418ec5887cb21d68b5c7924a49b3478` 接受修订 packet `44cb73d4…` 及原 runner。root 仅将状态改 ready，当前 packet SHA256 `15f1f2984fca662666b96f35835bdcb95c84b15223125a231e95bbe738bd3dbe`，并同步 REVIEW。已授权 Sol 仅创建两个新测试，产品三新文件须继续缺席，pytest/服务/迁移尚未运行。独立 root GO 生成脚本仅完成 AST 检查，尚未执行或产生 GO。

随后 root 重新检查 PG 完整容器 ID、名称、唯一命名卷和 loopback PortBindings，仅启动该专用测试 PG；当前 running、`127.0.0.1:59497`、`pg_isready` 接受连接。启动前核源 Qdrant 仍停止。未执行 pytest/迁移、未创建测试 schema 或用户，未连接源 Qdrant。

Sol 两测试冻结：unit `3b5d7c9da3ab8533529ec7cacec12aa1c02283bae407aed8468b73b023ee26b6`、schema `44b1b4b8e3d112ef4ab7c6254a7045b6a9889b0ff75a3795ec79ad4e50ea6ccc`。root 读测试及调用接口后独立生成 red-unit n1 GO `28610de1…`（1398项当前source-map），实际执行15.31秒。pytest exit2、ModuleNotFoundError `app.isolated_resource_settings`、JUnit 1 collection error/0 skip、source与governing未变、Job已空；runner completion `6da131ff82d06980842d439d5b090d72f39c58b89468e532224ab17bde76f29b` 的 accepted=false，原因 identity_ok 把正常 exit0 的 Git/conhost 辅助进程也当作必须 exit2 的 Python。此运行不计已接受 RED，禁止产品实现。Astra复核原绑定证据一致，Sol仅修运行器身份判定，待新SHA独审后以n2新命名空间重跑；n1证据完整保留。

运行器修正为 `acfc5c610a4a9af8a83231f2b1ec042143ab69060266fde2006ae59f1cf3b530`：四关键Python PID严格检查，辅助只允许额外pin的两Git/conhost精确镜像且exit0。Astra N2报告 `534a727927d7e1fc4f24088e84ab37cce7f28e9fcdc9bc982a3cdd0cd105900c`接受delta，root全文核。n2 unit RED接受，但schema预检时PG已停止，未启动pytest，失败completion `91fe5459…`保持原样、不计通过。root核精确容器及卷后重启PG，使用全新n3链重跑。

n3 unit RED completion `c1bb3f1e88fbac63bc78aaeb1925736df3e19474d72b0f2bd85a447a9964d1f3`；schema RED `73d5d5c82f00170c1bce7bf01e9840865da253e5e35c2402f3f06dd8d8f4a531`。两者accepted=true、pytestexit2、各1条真实missing-new-module collection error、0skip、identity_bound/source_unchanged/job_empty=true。schema前后精确PG健康且loopback59497。这是缺失实现的RED证明，无测试函数/fixture执行，不是migration业务通过。root已授权Sol High仅实施首包三新产品文件及runtime受限seam；两新测试冻结只读，产品完成待Astra源码独审和独立collect/GREEN，尚无实现验收/提交。

root 独立逐项核 n3 before/after/JUnit/reports 摘要及真正缺模块错误，组合RED记录 `init-t1-red-accepted-root-n3.json` SHA256 `b73d096703568e5e2c10fdf6a6da19682b910e3b56368b9ab8f12e92cdc2a09c`。Sol 首冻四产品：shared `6db0cb3a…`、trusted settings `07df248b…`、migration17 `40a9393d…`、runtime `806b2241…`；root 生成完整scoped diff `6a431c38dfed6820a38c4e725a6b2d6f6015dae8b7ee79a907ddae5133a5a104`。均仅AST检查，未运行GREEN或迁移。

Astra 源码 N1 报告 `12c3ac7d1bf84114fe1e0e3dd1f0a5510ffebd979d6fef9848a4ac10f4b1e361` 要求修正 R1触发器WHEN(false)绕过、R2 SQL字符串字面量语义被归一化抹去、R3数据库ledger未强制nonsecret endpoint。root接受具体构造并授权仅shared/schema测试修正。后续复审进一步确认scheme-less URL authority遗漏、profile负例需匹配真实registered identity才能归因URL、PG17 FK内部触发器enforcement必须检查、integer→smallint语义cast不能抹去。仍由同一Sol修正，等待最终delta冻结/独审；未标首包done，未运行GREEN/迁移，未提交。

Sol随后修订 shared `0630d6e4c991dc4a3fb128768ed3dd009c9bc4922cefa1aac3a63ffcc872f133` / schema测试 `2e57430b6ff7b94edca790ab6744e19afab05d42428932b4d647b2d2e9ce5368`，新完整review diff `d53caec8b9651730bb5e3c0a7082be3444ed0077990f1a4367189a43c0fb8c96`。Astra N3在审该freeze。root实证严格DDL文本比较仍误拒PG显示格式：TEMP表rollback诊断 `60b87545…`；首次一次性migration诊断因隔离stdin未加入repo路径失败并清理，记录 `162dcff5…`。修复诊断入口路径后第二次在新schema完整upgrade17成功，提取11条server canonical constraint定义，finally DROP并确认namespace缺席、source前后相等；记录 `init-t1-canonical-constraints-root-n2.json` SHA256 `112c1d55bc3986eae845ab906f896681f35053a4acd8101f4795183af2d9e4b3`。这是DDL/catalog专项诊断，不是GREEN、业务proof或功能验收。Sol正以该实际定义冻结严格比较基准，保留cast/literal/grouping及FK OID/列映射；无持久用户、无源Q连接。

Astra N3只余canonical误拒R5，报告 `d355d3ed…`；Sol修shared至 `0d91a95e…`，Astra N4报告 `ca349edfaffb2a4ee9610b05238ce71e9d9fd990ad70898458518e663bb1f5e4`静态接受，11servercanonical逐字一致。root正式执行n3 collect/unit：32测试全部PASS、0FES，unit completion `0b55be8fd2da877cc0c3136d51e731c63c8e4083fae3dd7f0a8295dc9002ba0a`。n3 schema37项中12PASS/25FAIL/0ERROR/0SKIP，94s，completion `e888c6943c1b4a7d3272025b4cba756a7999b1305127a4ca0042a1dd59df2ea0`、source/identity/jobempty均真。24失败为新fixture在实际SQL启动事务前调用witness.insert；另1为错误search_path下unqualified alembic查询抛UndefinedTable。原失败证据保留，不计schema接受。

Sol两localized修复：shared `68694ccc4aa5abb282c74651c363e9bf9d8a7a772524e3cef33ad1c2aaba504a`使用已验证quoted schema限定版本表；schema测试 `03e04c5408101cadf97d35971ada82816cf6627687bba251c83d498e58306715`在同cursor内read_current再insert。Astra `init-t1-green-fix-astra-n1.md` SHA256 `4029847f2b8274cbcedc389cf2df76fa6fb280cabb564fd3b2e71529e8f81706`接受精确delta。为完整保留失败namespace，私有runner增加显式root GO绑定的历史RED label/组合receipt引用；新n4将重新collect+全部GREEN，历史n3真实RED仅作历史依据，不能冒称当前新测试字节已RED。runner新冻结 `b28093831f681f86fd1ce044880b77f5e287ee73befe31495f8d84ecc1e4a693`待Astra独审，root独立GO builder已同步新字段但未执行。

## 首包验收完成，进入交付记录

retry runner获Astra接受（报告 `69932953cd183f4acacae2fe3d1a8c1ac289b97191d0984df0ddd2a64e0f3424`）。root新n4完整6阶段collect/execute均accepted：unit32、schema37、regression129零FES，129 distinct、198累计执行（含重复）、594 phase pass；三stage源码map相同 `aa78f654b264dfb8c9c0f2ab8090facc965d046854caa1e1f3c3555f429c89c6`。root组合receipt `df2d42df645ad36b63f98a6f3484b711e10fc60e8ab01d816e076f72761845cf`逐项重算SHA、exactnodes/phase/JUnit/源前后/15保护，现场核测试schema0、sourceQexited。

Astra最终 `init-t1-accepted-astra-n1.md` SHA256 `53615246d48210df450847f9ce44938fc6c398527fda09c765d175b76de5f791`独立验全部6stage和436当前源/1401map，SPEC/QUALITY accepted，仅Task01。root全文读报核hash并把packet标done，补实际handoff/REVIEW/spec/plan状态；修改前READY文档已另存runtime，不冒称原执行packet字节未变。此时尚未提交/推送。Task02–05未完成，不运行生产建户、普通bootstrap、main合并或部署。下一步先scoped交付首包，再生成基于实际接口的Task02草稿。
