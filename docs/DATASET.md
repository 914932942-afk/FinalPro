# IT 服务项目数据集：公开流程数据与合成诊断场景

本次数据位于 `evals/business_scenarios_v1/`。它服务于复现、边界验收和后续模型评测，不替换运行中的 `data/diagnostics/it_events.json`，不改动账号、密码、业务关联、工单数据库或有效规程，也不自动同步远端。

## 实际交付

| 内容 | 数量 | 用途与限制 |
|---|---:|---|
| UCI 498 原始事件日志 | 141,712 条事件，24,918 张匿名工单 | 真实公开流程观察；没有工单正文或 VPN/ERP 根因日志 |
| UCI 清洗后的工单前缀 | 400 张工单 | 研究历史状态、转派、等待、重开；后续观察单独存为标签 |
| Loghub 原始样本 | 8,000 行 | Apache、Linux、OpenSSH、OpenStack 各 2,000 行；没有本项目业务标签 |
| 合成诊断场景 | 500 条，50 个机制族 | VPN 22 族、ERP 28 族，各 10 个情境变体 |
| 合成数据可用性场景 | 80 条，20 个场景族 | 账号、日志源、权限、空记录、归属、会话和关联变化 |
| 合成工作流规格 | 372 条，93 个场景族 | 申请、身份、审批、执行、后台任务、人工处理和验收，各 4 个语言变体 |

合成部分共 **952 条、163 个场景族**。场景族是覆盖单位，变体数量不能当作独立真实事故数量。
其中 151 个场景族描述已有契约，10 个明确标为待实现能力，2 个标为待确定业务规则；“已有契约”也不代表每个场景已经做完整链路验收。

## 公开数据的来源和许可

UCI：Amaral, C., Fantinato, M., & Peres, S. (2018). *Incident management process enriched event log*. [UCI 原始页面](https://archive.ics.uci.edu/dataset/498/incident+management+process+enriched+event+log)，[DOI](https://doi.org/10.24432/C57S4H)，许可 CC BY 4.0。原始 ZIP、归属说明、清洗画像与文件摘要在 `third_party/uci498/`。

Loghub：[项目原始仓库](https://github.com/logpai/loghub)，固定提交 `dd61d0952749ee7963bde24220d1be5ede023033`。原始文件及上游 LICENSE 在 `third_party/loghub/`。上游要求研究/学术使用时保留许可和引用。该补充材料不自动适用本项目 MIT。

UCI 下载时 HTTPS 端点的证书验证失败。本次显式使用同一官方主机的公开 HTTP 数据端点，未关闭 TLS 校验、未发送任何凭据、未执行下载内容。实际 URL、传输方式和 SHA-256 记录在其 manifest；摘要表示本次下载内容完整性，不是发布者签名。以后可在 HTTPS 恢复后重新采集并对比摘要。

合成故障的技术参考来自 OpenVPN 官方排障资料、PostgreSQL SQLSTATE 文档、HTTP/JWT RFC 和 OpenTelemetry 演示故障。具体链接见 `sources.json`。没有复制文档长段落；账号、组织规则、请求、会话和诊断日志均为自编模拟。参考某个机制不代表厂商认可本项目策略。

## 合成数据覆盖范围

- **VPN**：DNS、路由缺失与重叠、证书过期与时钟偏差、信任链、TLS 超时、网关不可达、认证/MFA/锁定、MTU、代理、终端权限、链路不稳定、隧道正常但业务应用拒绝等。
- **ERP**：连接池、令牌受众、死锁、部署差异、403/502/503/504、数据库连接数、锁等待、取消、序列化冲突、磁盘满、重复业务键、缺表、缓存与 DNS 依赖、会话循环、令牌过期、413/429/422、模块故障、发布关联、维护及恢复。
- **证据情境**：当前会话、历史故障、较新的无关成功请求、UTC 与本地时间、记录乱序、心跳噪声、用户或日志恶意指令、证据缺失、发生时间不明确。所有诊断输入还含隔离的他人账号记录，工具不能将其返回给本人。
- **数据边界**：关联未配置/未绑定/重复/格式错误，源未配置/不可用/禁止访问，账号未覆盖，无记录或缺客户端/会话，记录归属错误、重复 ID、越权会话引用、快照后关联变更、业务账号停用时只读排查。
- **业务工作流**：澄清、否定/咨询/多诉求、第三方账号、高权限、登录状态变化、自审批/跨部门/过期审批、计划变化、重复提交、丢失响应、未知写结果、错误回执、多轮附件、后台超时/中断/迟到、认领/转派、补充信息、用户验收、复发和旧页面提交等。

未知故障即使包含明确错误，也不强行映射到现有规程。没有有效适用规程时，期望保留未知并交接；缺用户能够提供的事实时允许追问。数据质量和当前知识能力分开处理。

## 清洗和标签隔离

UCI 的处理保留原始 ZIP；字段去除首尾空白，`?` 和空值转为 null；日期按日/月/年解析，时区保持未知；按事件时间、更新计数和源行号排序，并记录时间相同的情况。重复投影记录保留，不擅自当作脏数据删除。

前缀输入只包含白名单字段。最终解决、关闭时间、解决人、关闭代码及事后 SLA 字段不放进输入；后续观察位于独立标签文件。富化数据中其他字段是否事后回填仍需复核，不能据此声称完成严格无泄漏的时序预测评测。

源状态不强制翻译为本项目状态；`Closed` 不等于“用户已验证恢复”。根因与人工评分保持 null。缺少正文、日志、时区的信息没有用模型补写成真实事实。

合成部分的 `inputs.jsonl` 与 `labels.jsonl` 分离，调用模型只传用户请求和授权工具观察。场景语义组固定分区，所有同组变体留在同一分区，防止把同一模板的改写同时用于调参与测试。分区仍由同一作者控制，不能冒充独立盲测。

## 当前验证能证明什么

`offline-check.json` 记录 **580/580 个只读适配器检查通过**，验证账号范围、快照关联与数据缺口。它没有调用模型、没有执行业务写操作，也没有执行 372 条工作流规格。

新增专项测试覆盖生成稳定性、标签泄漏、分区交叉、文件改动、无答案标签、时区/证书证据、实际适配器数据缺口、模型输入隔离和公开数据处理。

未运行真实模型评测；没有给出诊断准确率、自动解决率或独立人工评分。公开日志尚未训练或导入 RAG。

### 诊断时间与日志源边界

诊断链路先把用户给出的发生时间解析为带时区的 UTC 时间窗，默认前后各 15 分钟，最多允许 6 小时。只有落在该窗口内的客户端事件才会暴露给调查工具；服务端会话只能沿着这些客户端事件提供的 `session_ref` 继续读取。时间不明确时，HTTP 日志源会先追问日期、时间和时区，不会拿历史同刻记录代替本次故障。

默认 `AGENT_INVESTIGATION_SOURCE=file`，使用 `data/diagnostics/it_events.json` 做可复现的本地回放。设置为 `http` 后，诊断服务只调用 `AGENT_TELEMETRY_BASE_URL/v1/events/query` 的只读接口，并携带独立的 `AGENT_TELEMETRY_API_KEY`。请求中固定包含门户身份、资源、已映射业务账号和时间窗；适配器会校验账号范围、快照 ID、分页游标、记录归属、响应大小和源版本。项目提供的 `app.event_service` 只是本地模拟源，不能冒充企业生产日志。

本地回放的源标识、内容摘要、时区依据、时间窗和选中的记录都会写入诊断快照；HTTP 源还会记录传输方式与快照版本。日志源不可用、无记录、时间戳缺失、会话未关联、快照过期或结果被截断时，系统保留明确的数据缺口并交接人工，不生成确定性根因。

发送到外部模型前，工单文本、澄清历史、规程摘要和日志观察会经过固定规则脱敏。密码、令牌、授权头、Cookie、私钥、邮箱和手机号会被替换为占位符；错误码、时间、资源和事件关联仍保留。引用校验要求引用与模型可见的脱敏文本一致，因此脱敏不会放宽证据校验，也不会把原始秘密写入模型输出。

### 独立评测和人工复核

`scripts/check_business_dataset.py` 的报告现在包含整体和按场景族的契约通过率、追问率、数据缺口、引用越界、工具预算停止、重复工具停止以及有界时间窗比例。它仍然是离线适配器/结构检查；不会把合成标签传给模型，也不会把变体数量当作真实准确率。

对真实模型试跑，可把完成的 `live_diagnostic_subflow` 报告制作成盲评包。脚本会固定报告和输入文件 SHA-256，随机打乱案例，隐藏场景名、分区和期望标签，并要求评分者填写证据支持、规程适用、交接是否有用和不确定性是否保留四个维度：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_diagnostic_review.py prepare `
  --report evals/business_scenarios_v1/live-pilot-local.json `
  --inputs evals/business_scenarios_v1/inputs.jsonl `
  --output-dir evals/business_scenarios_v1/review-packet-local

# 评分者只编辑 review-packet.json 的 human_review 字段，再生成新文件后评分。
.\.venv\Scripts\python.exe scripts/prepare_diagnostic_review.py score `
  --packet evals/business_scenarios_v1/review-packet-local/review-packet.json `
  --reviewed evals/business_scenarios_v1/review-packet-local/review-packet-reviewed.json `
  --mapping evals/business_scenarios_v1/review-packet-local/private-mapping.json `
  --output evals/business_scenarios_v1/review-packet-local/score.json
```

评分脚本会拒绝篡改请求、证据或评测来源；未完成的人工项不会被算作通过。没有独立评分者、真实采集日志和故障注入时，报告只能说明可复现性与契约质量，不能声称生产诊断准确率。

## 复现

项目根目录：

```powershell
# 已发布目录不覆盖，重新生成时选择新目录。
.\.venv\Scripts\python.exe scripts/build_business_dataset.py build --output evals/business_scenarios_rebuild
.\.venv\Scripts\python.exe scripts/build_business_dataset.py collect-public --output evals/business_scenarios_rebuild

# 使用已有数据做不消耗模型 API 的检查，报告选择新文件名。
.\.venv\Scripts\python.exe scripts/check_business_dataset.py --dataset evals/business_scenarios_v1 --output evals/business_scenarios_v1/offline-check-local.json

# 可选：显式运行最多 10 个诊断案例，会产生 API 用量。
.\.venv\Scripts\python.exe scripts/check_business_dataset.py --dataset evals/business_scenarios_v1 --live --limit 10 --split challenge --output evals/business_scenarios_v1/live-pilot-local.json
```

HTTPS 确实不可用时可单独运行 `collect-uci --uci-public-http`；不必重新下载已成功的 Loghub。真实模型试跑只覆盖诊断子流程，沿用本机检索策略，输出结构契约得分与未填写的人工质量字段。

## 仍然不能覆盖的部分

有限数据无法穷尽所有企业、厂商、组织策略、事件交错和故障组合，也没有真实业务发生率。UCI 的流程统计是单一历史组织的观察，前缀样本特意覆盖边界，不能据其抽样比例推断生产分布。

原始数据实际出现 `Awaiting Vendor`、`Awaiting Problem`、`Awaiting Evidence`，以及未知状态 `-100`。这些提示后续可进一步设计供应商/问题管理依赖等待与异常状态处理，不代表当前项目已经支持完整状态机。所有源值保留在画像中，不悄悄归为正常状态。

后续最有价值的补充是：独立人员编写案例与评分，在可控环境真实注入部分故障并采集客户端/服务端证据，以及将工作流规格逐项映射到端到端验收。增加更多模板改写不能替代这些验证。
