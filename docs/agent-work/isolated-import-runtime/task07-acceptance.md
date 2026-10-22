# Task07：组合验收与限定分支交付

2026-10-22：**组合实现与验证已接受；限定 Git 交付待完成。** Sol High 负责实现及验证工具，Astra High 独立审查并发、一致性、运行器和实际证据。测试基点 `eebe89fe00b49895f05cec587f1dee75924e9ef1`；本轮仅更新治理文档，未改产品代码、测试、夹具、依赖或迁移。

## 本轮实际验证

固定解释器 `D:\python_self_agent\venv\Scripts\python.exe`，实际 base runtime `D:\Anaconda\python.exe` 身份另行绑定。35 个模块分四次严格串行执行，保持原阶段 3600 秒和清理 60 秒上限。

| 阶段 | 不同用例 | PASS phase | 阶段耗时/秒 | 实际保留内部退出0 | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| authority-a n2 | 329 | 987 | 2921.625 | 9 | PASS / Astra accepted |
| authority-b n1 | 234 | 702 | 1638.032 | 9 | PASS / Astra accepted |
| compatibility n1 | 247 | 741 | 277.110 | 10 | PASS / Astra accepted |
| isolated n1 | 210 | 630 | 731.235 | 9 | PASS / Astra accepted |

合计 **1020 个互不重叠的完整参数身份、3060 个唯一 setup/call/teardown PASS**，无失败、错误、跳过或内部错误。每阶段完整原 GO、before/after、XML、全部 phase、nonce/实际映像/创建时间/谱系/保留句柄退出、Job 清空与无剩余 owner 均由 root 和 Astra 分别核对。服务前后观察一致、无超时/截断，源 Qdrant 始终只检查停止且无绑定端口的 Docker 元数据。

真实 adapter 夹具经 `memory_publication -> publication -> shared_database -> command.upgrade` 执行迁移，其首用例 teardown phase index2 早于普通认证错误日志断言 setup index423，来自同一 pytest 进程；原认证断言未改且通过。

历史原始 430+207 JUnit 的 637 个完整身份全部保留，缺失0、新增383。收集1020与实际通过1020是不同证据，四阶段不是一次 pytest 调用。

## 输入与实现关系

原规划431输入实际为 **429 个 Git 跟踪文件加2个忽略的 Task02 验证脚本**。旧“431 tracked”是文档口径错误；未出现执行字节漂移。四阶段前后949项明确范围的输入及当前字节一致，不代表全安装盘动态依赖闭包。429个跟踪输入中139个原字节 blob 等于测试 HEAD，290个经 Git clean filter 等于 HEAD；原15个受保护文件原字节保留且不暂存。root组合收据 `p7-combined-root-n1.json` SHA256 `b87c7fce945dfd54635b1c2c15e616f8c0c82db6297799c912aa94d218b7b373`，记录每文件关系。

实际运行 packet SHA256 `13571653a70674c82ed9ec685a4304065174ce97b213978134c09cb1f1f04f82` 原件保存在忽略目录 `p7-packet-tested-root-n1.md`。后续 packet 状态、验收与输入标签的文档修改不是重新执行过的合同字节，测试选择与约束未变。

## 继承与中断边界

Task06 full75（51 controlled/unit、24真实进程）单独按原190输入核同：187相同，仅3份治理文档不同，无代码/夹具/provider漂移；本轮不机械重跑。75与本轮1020身份不重叠，但仅1020属于本轮实际执行。原96个精确资源absence是Task06继承证据，当前所有teardown通过不新增全局或逐运行资源absence主张。历史330/433/405/637不额外累计。

原 authority-a n1 在101个完整三phase后中断，缺 completion/XML/log/after，保留 `INCOMPLETE_UNKNOWN_EXIT`，不算通过或观察到的产品失败。Astra中断报告 SHA256 `62fb3c9c26c92472890c7799bd507109dd1ced95427819ca8c9827a813e34f03`。

四个后台顶层启动器最终退出未从原句柄观察，仍为未知；内部9/9/10/9个保留实际退出0和完整原结束回执构成已审接受边界。跨工具PID/creation/image观察不保证任意host/turn中断存活。`cleanup_elapsed_seconds`含后续snapshot/postflight/validation，不当作纯清理耗时。

## 独立证据

原件均在忽略目录 `.runtime/isolated-import-runtime/`，仅提交本摘要。

| 原件 | SHA256 |
| --- | --- |
| `authority-a n2` completion | `9bd3510e31d9c72d0bd2c411f23c5e4a5f8ef5cd2652795a3f7835d9a1ea262f` |
| `authority-a` Astra actual report | `44a00d914261b8093f9e80b994217a45f6b0289390c66995db6bc4a460f2380d` |
| `authority-b n1` completion | `a90fd3b17a80e1806ce752f66c922d333b0549332e03aaca1e36f45f8e014fee` |
| `authority-b` Astra actual report | `d77c49890360ade96044bd1095de736654ff2131c79158b2872da53a1d95feed` |
| `compatibility n1` completion | `4cab48a751bc1578d3dc427b1d0ee8bdf53308b3ddda647f23445b752d704b01` |
| `compatibility` Astra actual report | `aa880d82df9223900706443aac8bd2f540f7d95f43f45923dfa5e641be13cfd5` |
| `isolated n1` completion | `9d8fc3d29c54e9c79695c4f8962dfd29041cb9fa48ab890eb9e475f6044f3416` |
| `isolated` Astra actual report | `b496f57521a20f1693b59184a761fa334780fbf71031caaa5f0f5c4338b78ad7` |
| `p7-final-static-astra-n1.md` | `e6915f18d44a84abbfdb1415d308c87f306a8022dab96d5b92f309da3fd7b32d` |
| `p7-combined-accepted-astra-n1.md` | `3bc592f2bad997ae7d9544148d9665692695c455c51a3d8183ff9e92ed935ac5` |
| `p7-business-runner-astra-n2.md` | `a1dabce945aa5c2ee0e166852b223660e1e7053a28b73164cbf890c65b2b3dd7` |
| `p7-background-launch-astra-n2.md` | `fcadbe3b49c2533ae5a012958443cc64b2ccbc170daf1e961ce44a103fdbea75` |

Astra COMBINED SPEC ACCEPTED / QUALITY ACCEPTED，无blocking或纠正包；root全文读报。最终各模板审查项见同目录任务包的 `FINAL_INTEGRATION_REVIEW.md`。

## 交付及未覆盖范围

仅准备六份治理文档交付；尚未 commit/push 本轮文档，不将准备状态记成远端交付。测试源HEAD和其六个来源里程碑的祖先关系已核；当前远端授权分支读取仍为测试HEAD。交付时需记录实际commit/push退出、精确远端ref与文档字节。

未合并main或发布生产。新用户初始化、全业务迁移、历史向量忠实性、真实远程嵌入、不同镜像回退、多批次晚写/孤儿GC、真实七轮退避与全机/服务故障验收均不包含在本结论中。原close错误可见性及晚线程超出观察期限的限制保持。历史迁移继续保留文本与元数据，向量回忆标为待补证。
