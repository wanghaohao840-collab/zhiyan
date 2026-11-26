
# RAG 检索质量对照

题集含 8 篇合成文档、32 个带页码的片段和 36 道中英文问题。28 题有标注事实与证据，4 题缺少所问事实，4 题的答案位于未选文档。评测每题显式指定文档范围，用临时 JSON 检索库运行。

## 复跑

在仓库根目录使用项目虚拟环境。隔离工作树可复用主工作区的解释器。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_experiment --output-dir output/rag-quality-experiment
~~~

默认离线运行。用 `--mode auto --embedding-env-file <部署环境文件> --chat-env-file <聊天环境文件>` 探测所配置的服务并补充真实模型对照。两项服务都可用时运行全量。如果只有聊天服务可用，复跑固定 8 题，向量使用同一套本地回退实现。外部服务只收到合成语料和问题。环境文件原文、凭据和服务错误响应体不会写入报告。`--mode real` 要求两项服务可用。

报告汇总前 1、3、5 条命中率、平均倒数排名（MRR）、缺证据题拒答、引用回查和 P50/P95 延迟。`results.json` 保留逐题排名、回答正文、引用对应片段和计时。正确率按题集标注的事实和干扰值判定。

## 2026-11-23 首轮结果

| 本地回退向量，36 题 | 首条证据命中率 | 前五条证据命中率 | MRR@5 | 检索 P95 |
|---|---:|---:|---:|---:|
| 向量基线 | 64.3% | 100% | 0.801 | 5.680 ms |
| 混合检索 | 82.1% | 100% | 0.911 | 7.525 ms |
| 混合与多样性重排（MMR） | 82.1% | 100% | 0.900 | 11.954 ms |
| 混合与相关性重排 | 92.9% | 100% | 0.964 | 8.637 ms |
| 混合与两种重排 | 92.9% | 100% | 0.964 | 15.993 ms |

- 混合与相关性重排把首条证据命中从 18/28 提至 26/28。所有对照组均无越界。混合组在 8 道缺证据题上仍返回片段，向量基线为 7/8。
- BGE-M3 探针返回 HTTP 402。使用本地回退向量和真实 `deepseek-reasoner` 的 8 题子集，各组均答对 4/4 道可答题并正确拒答 4/4 道缺证据题。向量与混合加重排的完整问答 P95 分别为 2.038 秒、1.844 秒，每组 8 次请求。
- 相关性重排组固定上下文，加入引用要求后，正文引用覆盖由 0/4 增至 4/4。发出的 5 个引用全部能回查文档、页码及原文，其中 4 个命中标注证据。有一道拒答题附带了旁支事实的引用。
- 这些测量来自小型合成语料。离线生成器只抽取首片段，真实聊天组使用回退向量。服务延迟受当次运行和样本数影响。

`RAGTool.execute_result("search", ...)` 和 `RAGTool.execute_result("ask", ...)` 可传 `retrieval_mode="hybrid", use_mmr=False, use_rerank=True` 试用相关性重排。实现只处理作用域内前 24 条候选，根据稀有词项和相邻词项匹配调整排序。现有默认检索配置保持向量检索。

## 独立题集与逐条证据校验

从仓库根目录运行线上 BGE-M3 对照。省略聊天环境文件时只测检索。路径指向已经配置的本地环境文件。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_holdout --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-holdout-online
~~~

固定题集有 24 题和 64 个片段，覆盖改写、版本冲突、跨片段事实和缺证据。多证据题会单独统计前五条是否找齐所有必需片段。问答对照在同一个混合检索与重排配置上比较原有生成和证据校验。定量判断使用人工标注的事实到证据片段映射。

在工具的普通问答入口，选定文档后可传入 `verify_evidence=True`。应用服务可设置 `RAG_QA_VERIFY_EVIDENCE=1` 启用仅使用所选文档的普通问答。模型先选择逐字引文，再给每条事实绑定引用。程序检查范围、原文、数值、常用单位和代号，资料不齐时返回无来源的拒答。文本语义仍由模型判断，结构或引用校验失败时保留独立的问答错误状态。

### 证据校验延迟对照

在普通问答工具中，选定文档后可传入 `verify_evidence=True, evidence_strategy="single_pass"`，用一次模型调用同时输出原文与逐条结论。两种策略共用原文、引用、数字、常用单位和代号检查。旧调用仍采用双次策略。单次策略若遇到错误输出，最多再调用一次。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_holdout --grounding-latency --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-grounding-latency
~~~

使用同一份 24 道独立题和同一个聊天模型，交错执行两组。比较完整问答 P95、每题模型调用次数、原文引用覆盖、可答题准确率和干净拒答率。每题只做一次真实检索，问答使用远程 BGE-M3 的当次结果。报告独立记录可用性、质量及延迟，完整逐题输出保存在结果目录。

应用中可将 `RAG_QA_VERIFY_EVIDENCE=1` 和 `RAG_QA_EVIDENCE_STRATEGY=single_pass` 一起配置。校验开启时默认策略为 `two_pass`。

### 应用入口上线前验收

使用下一批预先标注的 16 道双语题和 40 个片段，从实际应用问答适配器进入，按应用的默认向量检索参数比较单次与双次校验。语料写入临时 JSON 索引。测试覆盖内存 Qdrant 中跨用户空间的同一文档 ID 隔离及来源字段。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_holdout --grounding-release --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-grounding-release
~~~

结果包含两组完整问答耗时、回查成功率、事实引用覆盖和额外背景引用。候选组需保持可答准确率和干净拒答率，并为全部标注事实提供对应证据。直接命中标注证据的引用占比需达到 95%，完整问答 P95 需低于同轮双次调用。脚本只运行隔离评测。

### 按问项选择证据校验策略

临时试用可配置 RAG_QA_VERIFY_EVIDENCE=1、RAG_QA_EVIDENCE_STRATEGY=adaptive 和 RAG_QA_EVIDENCE_MAX_TOKENS=4096。单次适用于单项问题。双次复核用于引号外带明显并列词或多个问号的问题。隐式多项问题仍可能走单次。自适应策略优先保留所有去重后的候选片段，空间不足时缩短片段正文。连片段最低长度都装不下时，返回容量不足。双次复核遇到空输出时，可在整次问答共用的一次重试限额内用较短指令重试。前两种固定策略仍然可选。输出上限留空使用现有生成默认值。提高上限会减少可用于引文和对话的输入预算。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_holdout --grounding-adaptive --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-grounding-adaptive
~~~

两组走应用问答适配器，均由适配器设置 4096 的输出上限。结果记录每次生成实际收到的上限以及每题选用的校验策略。合成短文本的对照结果需要结合长文本和应用负载验证解读。


### 分桶压力测试

固定 40 道中英文合成题，其中 10 道开发题和 30 道验收题分别使用独立命名空间。比较两次校验、单次校验和自适应校验，每组使用同一个模型与输出上限。报告按题型显示准确率、拒答、实际选用策略、输出截断、可见证据及全链引用。在线模型的逐次用量来自评测客户端。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_stress --split holdout --max-tokens 4096 --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-grounding-stress-holdout
~~~

可加 --bucket long_context 和 --max-tokens 1024 抽查长上下文的低预算表现。


### 验证码绑定和例外条件

启用应用证据校验后，可以额外设置 \`RAG_QA_EVIDENCE_CHECK_FIELDS=1\`。规则处理同一可见片段中同时出现明确的入口标识字段，以及单独给出或明确未公开的验证码字段时的错绑。引用仍保留模型逐字选择的原文。检测到错绑时共用证据校验的一次重试限额。例外条件的可能遗漏只在工具的诊断数据中标记。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_holdout --grounding-fields --embedding-env-file deploy/.env --chat-env-file .env --output-dir output/rag-grounding-fields-canary
~~~

两组用同一个模型和自适应策略运行 16 道合成验收题，比较答对率、干净拒答、引用、重试和延迟。字段校验的确定性效果用模拟生成结果的离线回放验证。


### 相似片段拥挤测试

每题从四份选中文档里检索 24 段内容，另有一份同命名空间的未选文档检查范围隔离。题集包含相近记录、版本冲突、易混字段、跨语言释义和跨文档联合证据。生成规则与指纹随代码保存，脚本会写出完整 JSON 供复查。20 道开发题与 30 道验收题分别使用独立题目、文档、正文和命名空间。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_crowding --split dev --smoke-limit 4 --pool-k 20 --embedding-env-file deploy/.env --output-dir output/rag-crowding-smoke
.\venv\Scripts\python.exe -m evals.rag_quality_crowding --split holdout --pool-k 20 --embedding-env-file deploy/.env --output-dir output/rag-crowding-holdout
~~~

基线、混合、混合加重排共享一次性预取的查询向量。向量与混合调用同一个检索入口，使用相同候选池上限和向量阈值，混合的词面通道仍扫描选中文档。混合和重排使用相同的候选列表，最后均取前五。报告区分候选池里缺少关键证据、关键证据已进候选池但掉出前五。计时将向量网络请求与本地检索分开，线上向量配置或请求失败时输出失败环节并退出。

给开发集命令加 --expanded-pool-k 24，可以在共享查询向量的前提下对照扩大候选池、用多样性重排减少近重复片段，以及两者合用。每组最终仍返回五条。报告会单列扩大候选池和多样性重排的耗时。

固定排序的问答回放使用已有冻结集的真实向量排名，各题型取固定中英文题。两组把各自前五条原文送入相同的问答适配器和聊天模型。结果保留独立的原检索耗时和回放问答耗时。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_crowding_qa --retrieval-dir output/rag-crowding-holdout-expand-mmr --chat-env-file .env --output-dir output/rag-crowding-qa-replay
~~~


### 跨规模拥挤测试

`--dataset scale` 在同一个检索评测入口加入 24、60、100 段三档合成资料。每个主题在同一组选中文档下有多种问法，涵盖同文档互补证据、语义近似的错误记录、跨语言证据、常规问法和答案仅在未选文档的题。排名命中率只统计有答案题。结果按规模和题型分组，保留候选池召回比例和被排除的原因。

~~~powershell
.\venv\Scripts\python.exe -m evals.rag_quality_crowding --dataset scale --split holdout --pool-k 20 --expanded-pool-k 24 --embedding-env-file deploy/.env --output-dir output/rag-crowding-scale-holdout
~~~
