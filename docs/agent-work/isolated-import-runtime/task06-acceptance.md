# Task06：独立进程导入验收

截至 2026-10-20，当前两份实现文件已完成完整新文件与固定回归两个阶段：75 项与 330 项分别通过，均无失败、错误或跳过。Astra 已独立接受两个阶段及 Task06 SPEC / QUALITY，无新增阻断。实现提交 `c225ad3b7086934adae80b5cd06dd9b9324e0bef` 已推送至 `codex/distributed-cutover-vertical-slice`，root 读取远端引用核同；后续文档交付记录见 progress。以下结果只适用于隔离专用 PG/S3/目标 Qdrant 与已记录的工作树输入，不证明 main 集成或生产切换。

## 实现与输入

实现由 Sol High 完成，Astra High 独立审查并发、一致性和实际证据。Task06 只新增以下实现文件，无产品入口、生产事务、迁移或远程嵌入接口改动。

| 文件 | 已测试工作树 SHA256 |
| --- | --- |
| `tests/integration/test_isolated_import_processes.py` | `53a0d8be05e5b303b2d9e2543a4953a59ba01389b7b715d6194d4f7bf9af81b9` |
| `scripts/verify_isolated_import_runtime.ps1` | `fd4a573406d489e07a4efd490273181e60a0cc8368d48d729cb20455d406005e` |

验证基点为 `380bbe777883594bcbad61ccbfa0e6c581689f6c`。固定项目解释器为 `D:\python_self_agent\venv\Scripts\python.exe`；其 Windows 启动器后实际运行的 `D:\Anaconda\python.exe` 身份另有 OS 映像、创建时间和谱系证据，不混淆两者。两个最终阶段各绑定 190 项输入并核前后字节；新 n6 回归 runner 相对已审 n5 只改三个轮次标签，其余逻辑一致。115 项既有基线输入与 15 个受保护未提交路径均保留。两组基线有重叠，不相加。

## 验收结果

| 阶段 | 实际结果 | 证据范围 |
| --- | --- | --- |
| Full final-n5 | 75 passed，0 failed/error/skipped，1988.497 秒 | 同一未筛选 pytest 进程；51 个受控/unit 身份和 24 个真实进程业务身份；225 个完整 PASS phase。root 原 session 93914 直接收取退出 0。 |
| Regression final-n6 | 330 passed，0 failed/error/skipped，700.418 秒 | 固定 11 文件、330 个完整参数身份，与收集清单严格一致；990 个完整 PASS phase。原 session 19811 后续不可接回；完整收据记录保留句柄的真实退出 0，不冒充 root 直接收取原终端最终退出。 |

两个阶段的用例集合不同，总计 405 个不同身份。历史分批运行、重复测试和失败轮不额外累计。Full 和回归的 runner 各有独立新 GO、输出命名空间和输入摘要；不能把不同 runner 自身造成的摘要差异称为同一原始输入字典。

Full 的实际业务覆盖：双 API 和独立 Worker 的文本/PDF 发布、跨 API 认证与 CSRF/租户隔离、API 死亡后继续处理、九个 Worker 死亡边界、同 PID 旧租约拒写、晚到候选字节、未知结果、恢复捕获及自然重试、锁等待后重新检查到期、队列捕获和双用户公平性。故障包装仅在测试进程内调用选定原方法；提交状态与原始物理对象/向量读取共同支持结论。自然 PostgreSQL 时钟控制租约和重试，不修改到期时间来制造业务结果。

Win32 子进程先挂起创建，加入私有 Job 并核验后恢复；使用受限句柄继承，保留实际映像、创建时间、谱系和退出码。先收束拥有的角色，再清理资源。Full 独审连接 77 个业务角色和 353 个外层保留退出身份，核对同轮控制、释放与 PG 等待证据。8 个受控生命周期记录中，极速 partial-assign 只有 inner 实际退出证据，不能虚构外层捕获；三个构造清理失败仍是 mock，不算真实进程业务用例。

Full 每个真实用例独享 schema、版本化 bucket 和两个 profile 限定集合，共 96 个精确资源。原 ledger 的创建/清理记录与 root 随后的只读 exact-absence 收据一致。源 Qdrant 始终只读 Docker 元数据，保持停止、无绑定端口，未访问其服务。回归沿用既有 fixture 断言和完整 teardown；共享 `p4-resources.jsonl` 混合历史数据，本轮没有启动前前缀快照，不把它声称为本轮精确资源映射或另一次 96-resource absence。

## 私有证据索引

以下文件位于忽略目录 `.runtime/isolated-import-runtime/`，保留原件，不提交运行缓存或凭据。

| 文件 | SHA256 |
| --- | --- |
| `p6-baseline-root-n1.json` | `5c58db379a9da3f20b18f8c1dd5138ffd412915a6699ca9ee7edab720bb0d5bb` |
| `run-p6-sol-final-n5.py` | `c284ca620ee7915b6c1d3a4d2915246a7b33bf79c8a4b085aa7d394febad3438` |
| `p6-full-final-n5-completion.json` | `32092c8787da8b2d6621ba4dd01eb4134f36af3c6f5652863563c3f6bcc2afe9` |
| `p6-full-final-n5.xml` | `cb6a5616c271057fecdd280f962e4e3d3d341e41a4c298b38abca53134e44d4d` |
| `p6-full-final-n5.log` | `da75498cb0d2b2afe15da1da871aca0a4e8ea82aec58a21326bd035ffd4aa8fc` |
| `p6-full-absence-root-n5.json` | `e2a332497d1006cc2cbcdb42d9f59f2548b97f3cc3aad9932fb69f11f12db7f4` |
| `p6-full-accepted-astra-n5.md` | `f805d79d471024be4b8c156063df9b8ead48a568770b766550058f5ff441e7f9` |
| `run-p6-sol-final-n6.py` | `8bf1fba07e75fb374ce3e2abe0fc9915abe6d3e9f041b8e1fce3a210f42d7a70` |
| `p6-final-n6-frozen-rawmap.json` | `ca26f89c570d746b76acb6841128a1c1681daecaaf59016a2752ec713dc92dc9` |
| `p6-final-runner-astra-n4.md` | `ad09a35426380666415920657119bf239ab70f941a4db849afe33fd3fcc3ed68` |
| `p6-regression-go-root-n6.json` | `1ef023d732a983397dd7e7724e12cc591b692b587eb5f4d9e003c4f22a360bd5` |
| `p6-regression-identity-root-n3.json` | `74d903fd2c368a2ad198f6e2ca0e52505ce8057a3e0451ef99537502b0c44b52` |
| `p6-regression-final-n6-completion.json` | `f702d0b3d7fd01e0b7090c6346aced10859136a0f01c80d69dcdbb752cd38da9` |
| `p6-regression-final-n6.xml` | `fdaf35b526ea4c8ea262934b33b5bdbf535fc67c1b47f5a30fadd73517b89a8d` |
| `p6-regression-final-n6.log` | `917da18e242f142b604a4292c2febcae80fdbde39380b08a6427d122be56b75d` |
| `p6-regression-final-n6-report-transforms.jsonl` | `ba883f8966fd24f1d73381af75f95bd9e3948634eafd253ce34334a0253453b2` |
| `p6-final-accepted-astra-n6.md` | `6ec687d64b0db752634dff0566bd533763be28a0b051e05c4dbab332d87c612f` |
| `p6-final-audit-astra-n6.json` | `ee14d500bbd38bb435e0549c9d175679f7d5b3e9c063be2c5fbd2c4407d35bbb` |

完整的阶段命令、场景/控制/资源/进程身份和各次审查链接见 [progress.md](progress.md) 与 Task06 packet。最终独立数据审计实际退出 0，共 2409 项检查。独审不重新运行候选函数或把 root 的服务观测写成第二次独立服务观测。packet、REVIEW 和计划随后更新为验收记录；这些文档交付字节与测试时绑定版本不同，不宣称经过 pytest，也不改变两份已测试实现文件。

## 保留记录与未完成范围

原 Full n4 和回归 n5 保持 `INCOMPLETE_UNKNOWN_EXIT`，不补写退出、完成或 teardown；n4 事后精确四资源清理另有审查和 absence 收据，不替代原运行 teardown。更早真实 fairness n2 是已观察失败，原件保留；修正后的 n3 和最终 full n5 另行接受，不追认旧轮通过。

Docker Desktop 的过期 socket 目录已通过可回退的改名保留并恢复当前引擎与专用测试服务，未重置数据卷；这不证明上游故障永久消失。该维护与 Task06 两文件提交范围分开。

本阶段不宣称整机/服务故障覆盖、多批次晚写、孤儿资源 GC、七轮真实退避/manual-hold、历史向量可忠实迁移、Task07、main 集成或生产发布。历史迁移继续按用户决定保留文本和元数据，向量回忆待补证。上述 Git 交付是独审后的新增观测，具体范围和远端核验记录见 progress，不追认为 pytest 证据。
