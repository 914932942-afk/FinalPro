# IT 服务受理与证据调查 Agent

个人 Agent 开发求职项目：可信身份受理、受限规划、审批、幂等登记，以及有预算、可恢复的 VPN / ERP 只读调查。
日志与业务对象是模拟数据；DeepSeek 可真实调用。工作台模式通过真实 HTTP 向独立演示服务登记事件与附件，权限动作仍为本地模拟。当前版本不声称修复了企业系统。

启动与当前功能入口见 [README](../README.md)，页面、HTTP 契约和演示步骤见 [工作台说明](WORKBENCH.md)。本文保留架构、配置与实验说明。

## 当前架构与业务边界

```text
认证身份 → intake → clarify → retrieve（策略目录）→ plan → authorize → execute → finalize
                  ↑ 用户补充／敏感计划确认              ↑ 独立审批          │
                                                                          ├─ 普通任务完成
                                                                          └─ 事件已登记、调查排队
                                                                               ↓ 后台或显式恢复
                                                                        diagnose（有界 ReAct）
                                                                               ↓
                                                                        enrich（独立幂等附件）
                                                                               ↓
                                                                        交接人工 → feedback
```

- 外层 LangGraph 组织节点顺序与条件路由；业务代码结合 SQLite 快照实现暂停与恢复，SQLite 同时保存审计及操作账本。
- 模型解释用户表述、提出受限计划、选择关联会话及规程查询。程序控制身份、动作白名单、确认、审批和副作用。
- 权限申请仍需申请人确认（模型路径）及独立审批，账号来自后端身份；诊断不会生成授权。
- `open_incident` 成功后先返回 `awaiting_investigation`。调查不在登记操作之前运行。
- API 在响应发送后调度调查，启动时扫描持久化的 queued/running 调查。后台调度是单机轻量机制，不是持久消息队列；断电后可通过重启或恢复接口继续。
- CLI 不常驻后台：`run` 返回受理结果，随后执行 `investigate 工单ID`。
- 创建事件与 `attach_evidence` 分别使用幂等账本。任何写操作结果不明都转人工，不盲目重放。
- `closed` 仅表示 Agent 任务交付；`resolution.external_issue_resolved` 初始为 null。只有独立处理人的反馈才能记录外部问题是否解决。

## RAG 为什么存在

固定身份和审批规则由程序保证；明确的内部错误码规程通过策略目录精确查询，不再强制依赖向量检索。
`planning_context` 返回所有命中适用错误码的策略片段，避免 top_k 隐藏冲突；普通请求使用固定的通用 SOP。

真正需要检索的内容是：当前故障适用的排障步骤、前置证据、排除条件和交接要求。
主流程与排障知识复用 `KnowledgeBase` 文档加载、元数据和分词能力，按用途隔离；诊断由 `RunbookKnowledge` 提供版本化规程。

当前 `data/knowledge/runbooks/` 有 16 份规程：12 份 active 基础故障调查规程，以及 4 份已纳入版本治理、暂不参与召回的 retired 扩展规程。
例如，相同连接等待现象在 cloud-proxy 与 classic-direct 部署下，需要收集不同指标并交不同支持组；RAG 提供这类模型预训练并不知道的本地知识。
所有内部约定明确标注为自编模拟，不冒充厂商文档。

- 每份短规程整体返回，包含适用条件、证据和交接内容；不把操作建议与前置条件切开。
- 元数据记录 document_id、resource、service、environment、family、version、status、owner_team、severity、reviewed_at、provenance。超长规程须人工拆成完整适用单元。
- 程序检查资源、引用原文、显式部署标识以及声明的同范围版本冲突。任意自然语言的正确性不是程序已经证明的事实。
- 同一 family/environment 的多个 active 版本视为冲突；先退役旧版本，再发布新版本。冲突检查针对整个资源知识视图，不只检查 top_k。
- 默认使用 **hybrid**：先过滤当前不具备适用条件的部署规程，再用 BM25 和带查询指令的 BGE 召回，并通过 RRF 融合候选。`vector` 和 `bm25` 仍可作为对照；向量失败会显式记录 BM25 降级，不能把降级结果计作向量成功。
- 融合候选按文档 ID和同一适用范围内的规范化正文精确去重；有可用 embedding 时按语义相似度做保守去重。不同 resource、environment、family、version 的规程不会因相似而删除。
- RRF 后支持可选 Cross-Encoder 重排。重排模型只读取本地预置权重；模型不可用时保留 RRF 顺序并在 `retrieval_status.rerank` 中记录原因。
- 部署环境未知或观察冲突时，仅召回通用规程；只有后端日志明确且一致地标记环境时，才加入对应部署的规程。生成的查询不能伪造环境。最终引用校验也拒绝忽略已观察到的环境冲突。
- VPN / ERP 调查共享一个延迟加载的 BGE 模型；按模型与正文内容缓存最多 256 个文档向量、64 个查询向量。缓存不保存授权或调查结论，文档元数据始终来自当前调查快照。
- 小型冻结知识视图直接对 BGE 向量打分，不额外维护诊断 Chroma 索引；Chroma 后端保留给规划和检索实验。hybrid 会在冻结视图上融合 BM25 与 BGE；显式选择 BM25 或发生降级时，不能将该次运行描述为向量成功。
- 新调查读取最新规程；进行中的调查使用已持久化版本，规程修改不会偷偷改变其证据。
- `data/diagnostics/runbooks.json` 仅供历史实验，不是当前生产调查的知识来源。

### 历史方案与验证：适用范围优先的向量检索（v3）

该部分记录的是 v3 版本的历史结果。当前版本在此基础上增加了候选去重和可选 Cross-Encoder 重排，历史数字不代表当前版本指标。
在已有 32 查询开发集上，比较了范围过滤、文档表示、查询指令与融合方式。删除重复文段降低召回，放弃；修改 RRF 常数没有收益，放弃；直接 Transformers 加载没有观察到冷启动优势，仍使用现有 SentenceTransformer。
开发集按 Recall@2、MRR、实现复杂度依次选型：范围过滤 + 查询指令的纯向量方案召回 25/28，与更复杂的词项优先融合并列最佳，选更简单的纯向量。生产代码仍返回完整原文，不删适用条件或证据。

选型后另写 24 个验证查询（20 个有指定规程、4 个无适用答案），冻结检索方案后运行，未根据这些查询改写知识库或调参。它们仍是开发者自编的模拟数据，不是独立企业盲测。

| 方法 | 新验证集目标规程 Recall@2 | 10 个有答案案例的暂定有据交付 | 2 个无答案案例安全停止 | 12 例生成 token |
|---|---:|---:|---:|---:|
| 旧 BM25 | 14/20 | 5/10 | 2/2 | 16380 |
| 旧 BM25+BGE/RRF | 17/20 | 7/10 | 2/2 | 18565 |
| 改进范围过滤的 BM25 | 15/20 | 5/10 | 2/2 | 15636 |
| **v3 向量方案（历史）** | **19/20** | **9/10** | **2/2** | **18678** |
| 当前适用范围内全量规程 | 20/20（全部提供） | 10/10 | 2/2 | 25805 |

当前 v4 本地检索复核（`data/evaluations/retrieval-v4-local-20261011-final-kb12.json`）中，BM25 top-2 为 17/20、BGE vector top-2 为 20/20、BM25+BGE hybrid top-2 为 20/20，hybrid MRR 为 0.925，范围泄漏为 0。该复核使用自编冻结查询，不是独立企业盲测；本次未预置 Cross-Encoder 权重，因此重排按 optional 策略安全回退到 RRF。

“有据交付”同时要求：语义评审通过、明确选择适用规程、引用指定规程与当前日志、引用逐字存在、未宣称完成修复。一般建议不再算作完成规程交接。无答案要求明确 insufficient 且不指定规程。
10+2 个真实生成案例在验证文件中预先选定，并非 24 个全部做了生成评审。语义评审仍为另一次同模型调用，人工复核字段为空。新向量组有 1 次评审输出格式失败，仍计未通过；没有通过重试补为满分。
初始打分把“尚不能确定根因或已完成修复”误判为宣称修复；已补正反例测试，并对所有方案的原始答案和原始评审统一重新计分。没有改写答案、标签或再次生成；原始记录与重新计分文件同时保留。

当前向量方案相对全量规程减少约 27.6% 生成 token，但全量规程本次交付得分更高；因此这里选择的是两份规程预算下的质量/成本折中，不声称全局最优。向量方案仍漏召回 1 个有答案查询，失败记录保留。
本机 CPU 新向量方案热模型未缓存查询 P95 约 17.9 ms，首次检索约 14.5 s，进程峰值约 539 MiB。冷启动未被消除，各组首次只测一次，不能将本轮 14.5 s 与上一轮 16.6 s 差异当成稳定加速。
启用仅本地加载后的独立复核仍为 19/20 召回、零降级，热查询 P95 约 18.4 ms、首次约 16.9 s；因此冷启动应按本机约 14–17 s 理解，不能承诺小于 15 s。
实际接口 7/7 场景通过，包括首次约 14 s 的真实向量加载，调查在既有 60 s 预算内完成；确认使用 vector 且没有退化为 BM25。先登记后调查、独立幂等附件、身份隔离及原有审批边界保持。

最终回归 **224/224 通过**；日志：`data/evaluations/retrieval-v3-final-local-regression-20260929.log`。

原始证据：

- 开发消融：`data/evaluations/retrieval-v3-development-20260929.json`、`retrieval-v3-fusion-development-20260929.json`。
- 选型冻结：`data/evaluations/retrieval-v3-selection-20260929.json`。
- 新查询召回与性能：`data/evaluations/retrieval-v3-release-20260929.json`。
- 同提示词真实生成：`data/evaluations/retrieval-v3-grounded-live-20260929.json`。
- 原文统一重新计分：`data/evaluations/retrieval-v3-grounded-rescored-20260929.json`。
- 完整接口链路：`data/evaluations/retrieval-v3-http-20260929.json`。
- 本地权重复核：`data/evaluations/retrieval-v3-local-weights-final-20260929.json`。
- 最终选型与文件指纹：`data/evaluations/retrieval-v3-final-decision-20260929.json`。

复现时使用新输出文件名：

```powershell
.\.venv\Scripts\python.exe -m app.retrieval_experiment --fixture evals/retrieval_validation_v3.json --variants legacy_bm25 legacy_hybrid bm25 vector full_context --output data/evaluations/my-v3-retrieval.json
.\.venv\Scripts\python.exe -m app.knowledge_experiment --live --fixture evals/retrieval_validation_v3.json --live-subset --variants legacy_bm25 legacy_hybrid bm25 vector full_context --output data/evaluations/my-v3-live.json
.\.venv\Scripts\python.exe scripts/check_investigation_workflow.py --live --include-scope --runbook-strategy vector --output data/evaluations/my-v3-http.json
```

查询指令仅对当前 BGE-small-zh-v1.5 应用，不会擅自套到自定义 embedding 模型；文档不加指令。依据 [BGE 官方模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5) 的短查询/文档编码约定，收益仍以本项目实验为准。

### 上一轮记录（历史 v2，非当前默认配置）

新增 32 个冻结的自编查询：精确术语、口语改写、中英文表达、部署条件、历史/否定干扰及无答案。
28 个案例有指定规程，另 4 个没有适用答案。它们不是企业盲测数据，没有为这些查询改写知识库。

| 方案 | Recall@2（28 个有答案案例） | 热模型、未缓存查询 P95 | 首次检索 | 进程峰值内存 |
|---|---:|---:|---:|---:|
| BM25 | 19/28 | 0.54 ms | 1.85 ms | 37 MiB |
| BGE 向量 | 18/28 | 16.62 ms | 13.28 s | 549 MiB |
| BM25 + BGE/RRF | 20/28 | 16.18 ms | 16.62 s | 549 MiB |

混合检索对中英文组的召回从 2/6 提升到 3/6；口语组仍为 2/6。并非所有表达变化都获得提升。
逐例看：`mixed-v3`（凭据超出日期范围的英文说法）和 `negated-e1` 获得目标文档，`negated-v2` 却丢掉 BM25 原本召回的目标文档；净增 1 例不意味着没有退步。向量排序不能代替模型对“历史错误已恢复、当前故障不同”的判断。
top_k=3 时混合检索召回 23/28，但平均上下文约 760 字符，高于 top_k=2 的 506 字符。
全量规程能覆盖全部目标文档，但平均上下文约 1519 字符，不能将这种“召回率”当作最终答案准确率。

预先指定的 12 个案例进行了真实 DeepSeek 生成与独立调用评审：四组均暂判 12/12，通过率没有提升。
生成 token 为 BM25 15172、向量 17255、混合 17437、全量上下文 30946；混合相对全量减少约 43.7%，但相对 BM25 增加约 14.9%。
这里允许模型在知识缺失时给出谨慎的一般建议，因此未召回指定文档仍可能通过语义评审；该结果不能证明 RAG 提升任务成功率。
语义评审来自同一个模型的另一次调用，人工复核仍为空；拒答评估仅包含其中 2 个无答案案例。检索阶段返回候选不等于生成了错误答案。

补充同一预选集合上的 hybrid top_k 对照，生成与评审均使用相同设置：

| top_k | 全部 28 个有答案查询召回 | 12 例暂定语义通过 | 12 例生成 token |
|---|---:|---:|---:|
| 1 | 15/28 | 12/12 | 13385 |
| 2 | 20/28 | 12/12 | 17437 |
| 3 | 23/28 | 12/12 | 21139 |

增加候选提高了指定文档召回，但未观察到最终建议通过率提升。保留运行链路最多 2 份规程，作为召回与上下文开销的折中，不声称它是全局最优。k=1/3 的原始记录分别为 `hybrid-topk1-live-20260929.json`、`hybrid-topk3-live-20260929.json`；复现时给知识实验命令追加 `--top-k 1` 或 `--top-k 3`。

上一轮设定的默认切换条件包含首次向量检索不超过 15 秒，混合检索当时未满足；也未观察到最终建议质量改善。因此当时保留 BM25 默认；当前已按上方 v3 的新证据切换，历史数据不删除。
以上性能来自本机 CPU、独立进程，首次检索包含模型导入和加载，每组首次检索仅测一次；操作系统文件缓存不受控制。不能视为普遍延迟保证。

复现命令（输出文件必须是新文件，保留历史结果）：

```powershell
# 本地真实 BGE 检索；不调用生成模型 API。比较 top_k=1/2/3、范围隔离、冷/热查询及内存。
.\.venv\Scripts\python.exe -m app.retrieval_experiment --output data/evaluations/my-retrieval.json
# 12 个预选案例 × 4 组；需要 DeepSeek API，包含生成和评审调用。
.\.venv\Scripts\python.exe -m app.knowledge_experiment --live --fixture evals/retrieval_probe.json --live-subset --variants bm25 vector hybrid full_context --output data/evaluations/my-live.json
# 临时应用真实调用混合检索，检查受理、审批边界外的调查、幂等附件与越权访问；不修改默认配置。
.\.venv\Scripts\python.exe scripts/check_investigation_workflow.py --live --include-scope --runbook-strategy hybrid --output data/evaluations/my-hybrid-http.json
```

原始记录：`data/evaluations/retrieval-probe-20260929.json`、`retrieval-live-20260929.json`、`hybrid-http-20260929.json`。
HTTP 首轮覆盖 7 个既有 VPN / ERP / 分支 / 部署场景，6/7 通过且确认实际使用 hybrid、未降级；t04 在上游澄清校验处发生 ValueError 并安全停止，没有进入检索或写入。这是保留的真实失败，不能归因于向量检索，也不能用后续复测覆盖首轮分数。
同一 t04 输入单独复测 1/1 通过，见 `data/evaluations/hybrid-http-t04-recheck-20260929.json`；复测未放宽校验，也不表示模型波动已被消除。默认策略的事先验收条件、逐例增减和运行环境保存在 `data/evaluations/hybrid-default-decision-20260929.json`。
最终回归 215/215 通过，日志为 `data/evaluations/hybrid-release-regression-20260929.log`。覆盖共享服务、并发缓存、文档新增/变更、冻结快照、部署隔离、向量故障降级及原有身份/审批/幂等流程。

## 调查与证据

工具只有 `client_events`、`session_events`、`search_runbooks`。session 必须先出现在当前账号已读取的记录中；不能传其他账号、路径、SQL 或写工具。
默认由程序预取第一份客户端日志，仍计入 5 次工具预算；模型决定后续会话、检索查询与停止时机，最多 6 次模型调用、60 秒。
预取不会额外增加首步查询，也不会在恢复时重置预算。

调查开始时冻结当前账号、当前资源的日志和适用资源知识。每条观察保留 snapshot_id、captured_at、observed_at，日志能解析原始时间时另存 event_at。
当前适配器冻结账号内的有限本地日志集合，**没有宣称接入实时日志平台或实现通用时间窗口查询**；旧错误与当前会话的关联仍需模型判断并评测。
其他账号日志更新不会中止当前调查。新调查能看到更新；旧调查重启后继续使用旧快照。

`evidence_verified=true` 仅表示引用完整性校验通过；`verified=false` 表示未把诊断假设当作已证实根因。
语义正确性、规程适用性、建议有效性和人工反馈分别记录。

## 安装与配置

Windows PowerShell，在项目根目录：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env  # 仅首次；已有 .env 不要覆盖
.\.venv\Scripts\python.exe -m app.auth init
# 首次使用 vector/hybrid 前准备本地权重；已有 data/models/bge-small-zh-v1.5 时无需重复下载。
.\.venv\Scripts\python.exe scripts/download_embedding.py
```

身份由服务端本地配置确定。门户使用账号密码及服务端会话，CLI 使用随机 API Key；服务端保存密码哈希与 Key 摘要。初始密码在本机 `data/auth.accounts.txt`。不要提交本机 `.env`、认证文件、初始密码文件或会话库。登录方式详见 `WORKBENCH.md`。
客户端发送 `Authorization: Bearer <API Key>`；不能通过传入 username/admin/role 获得权限。

`.env` 中配置真实模型：

```dotenv
AGENT_LLM_MODE=deepseek
DEEPSEEK_API_KEY=填写自己的密钥
AGENT_DIAGNOSIS_MODE=react
AGENT_DIAGNOSIS_BACKEND=investigation
AGENT_RUNBOOK_STRATEGY=vector
AGENT_DIAGNOSIS_PREFETCH=true
AGENT_DIAGNOSIS_BACKGROUND=true
```

保留已有模型名、地址与超时配置即可。离线模式提供规则演示，不能当作真实模型实验。
常用路径默认为 data/agent.db、data/knowledge、data/knowledge/runbooks、data/diagnostics/it_events.json。
vector/hybrid 可通过 `AGENT_EMBEDDING_MODEL` 指定本地 BGE 路径；优先自动发现 data/models/bge-small-zh-v1.5。
调查请求只从本地目录或本地缓存加载权重，不在工具调用中联网下载；权重缺失会记录 BM25 降级。修改配置后需重启已运行的服务，新调查才使用新设置。

```powershell
.\.venv\Scripts\python.exe -m app.api
# http://127.0.0.1:8080/docs
```

`DEMO_API_KEY` 环境变量供 CLI 使用，值为所选预置身份的随机 Key。命令参数不接受自报角色。

```powershell
.\.venv\Scripts\python.exe -m app.cli run --title 'VPN 故障' --description 'VPN 连接失败，报错超时' --idempotency-key demo-001
.\.venv\Scripts\python.exe -m app.cli investigate 工单ID
.\.venv\Scripts\python.exe -m app.cli operations 工单ID
.\.venv\Scripts\python.exe -m app.cli recover 工单ID
# 独立经理或管理员的 Key：
.\.venv\Scripts\python.exe -m app.cli feedback 工单ID --revision 1 --verdict accepted --note '人工核查后采纳建议' --evidence '核查记录编号' --issue-resolved unknown
```

## API 契约

| 操作 | 接口 | 权限与行为 |
|---|---|---|
| 提交 | POST /tickets | 可信身份；支持 Idempotency-Key；返回原始受理快照 |
| 最新状态 | GET /tickets/{id}/state | 本人、同部门经理或管理员 |
| 补充信息 | POST /tickets/{id}/clarify | 仅本人，answer + round_number |
| 审批 | POST /approvals/{id} | 独立授权人，approved 布尔值 |
| 调查／继续调查 | POST /tickets/{id}/investigate | 有工单访问权限；不重新创建事件 |
| 反馈 | POST /tickets/{id}/feedback | 独立经理／管理员；不能验证自己的工单 |
| 恢复／查看账本 | POST /tickets/{id}/recover；GET /tickets/{id}/operations | 授权经理／管理员 |
| 取消 | POST /tickets/{id}/cancel | 仅本人等待澄清或审批期间；不能取消已执行动作 |
| 人工结案 | POST /tickets/{id}/resolve | manual_review/failed；结果未知时必须有核查凭证 |

反馈请求：

```json
{"revision":1,"verdict":"accepted","note":"核查后采纳","evidence":"人工核查记录","issue_resolved":null}
```

verdict 为 accepted/rejected/inconclusive；issue_resolved 独立表示外部问题是否解决，不与采纳建议混为一谈。
反馈追加保存并绑定原始报告哈希；相同版本相同内容幂等，改内容或跳版本返回 409。反馈不会训练模型，也不自动写回可信知识库。
重复提交返回最初响应（可能仍是 awaiting_investigation），最新状态须 GET 查询。

## 测试与实验

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 -m app.knowledge_experiment --live --repeats 2 --output data/evaluations/my-knowledge-run.json
.\.venv\Scripts\python.exe -X utf8 -m app.investigation_experiment --live --split branch --strategies react react_prefetch --output data/evaluations/my-prefetch-run.json
.\.venv\Scripts\python.exe -X utf8 scripts/check_investigation_workflow.py --live --output data/evaluations/my-http-run.json
```

实验输出必须使用新文件名，保留失败运行。`--live` 会产生实际模型用量，业务写入仍是模拟。
新知识实验固定日志、模型、生成提示词，仅改变知识提供方式；比较无知识、旧规程、BM25、混合检索和全量资源上下文。
语义评分使用另一次模型调用，并有引用完整性与“已修复”声明检查；**模型评审是暂定结果，不等于独立人工标注**。human_review 留空供人工复核。
generation 与 judge 用量分开；未知用量不计作零。标签与参考规程只传给评审器，不传给生成模型。
较早的 A–H 实验使用 6 个开发案例，重复两次不是 12 个独立场景；v2 的 32 查询开发集与 v3 的 24 查询验证集、12 例预选生成实验见上方“当前方案与验证”。两者不能拼接成盲测或真实企业收益。

前一轮结果与限制见 [A–H 验证记录](VALIDATION_AH.md)。历史实验原始输出保留在 `data/evaluations/`，对应各次实验的版本与日期，不代表当前默认流程。当前架构和运行方式以 [README](../README.md) 及 [工作台说明](WORKBENCH.md) 为准。

## 主要文件

| 文件 | 职责 |
|---|---|
| app/workflow.py | 受理、授权执行、后台调查及附件交付 |
| app/service.py / app/auth.py | 身份与工单访问、独立审批和反馈权限 |
| app/knowledge.py / app/runbook_knowledge.py | 策略目录、版本化规程、检索与范围冲突 |
| app/investigation.py | 只读 ReAct、冻结证据、预算、引用检查 |
| app/tools.py / app/action_adapter.py | 创建和更新两种独立副作用的幂等契约 |
| app/audit.py | SQLite 快照、审计、操作账本、进程锁 |
| app/knowledge_experiment.py | 知识消融与暂定语义评分 |
| app/retrieval_tuning.py / app/retrieval_experiment.py | 开发消融、冻结查询召回、冷/热性能与 top_k 对照 |
| evals/retrieval_probe.json / evals/retrieval_validation_v3.json | 历史开发查询与选型后新增验证查询 |

## 兼容性与限制

已有字段、历史已完成快照和原请求幂等响应保持可读。新增字段有默认值。
旧版在登记前中断的 diagnose 快照不能直接套用新版顺序，明确要求人工核查后结案；不擅自迁移并重放副作用。
旧版未保存证据快照的调查不会混用新日志继续推断；保留原进度并安全结束。
未知附件写入通过人工核查结案，不自动换键再发一次。
这是单机、预置身份、模拟适配器的个人项目。实际运行须替换身份和日志／工单适配器，并验证下游幂等契约；并未实现企业 SLA、多实例队列或真实修复。


