# 工作台与 HTTP 演示服务

## 目的与范围

页面把已有的 Agent 流程变成可交互的业务演示：员工提出问题、补齐信息，经理确认具体审批计划，处理人核查证据并反馈。浏览器没有身份决策、审批授权或模型执行权限，所有规则仍在后端。

前端是 FastAPI 同源提供的静态 HTML/CSS/JS，无需 npm、前端路由框架或额外登录后端。选择这个方案是为了让状态、证据和人工接管可见；不为了展示前端技术而引入额外服务。

## 启动

已有虚拟环境与 `.env` 的机器双击根目录 `run_workbench.bat`，或在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m app.workbench
```

- 页面：`http://127.0.0.1:8080`，接口文档：`/docs`。
- 独立下游：`http://127.0.0.1:8091`，持久化到 `data/demo_service.db`。
- 首次运行若缺少身份配置，初始化四个本地身份。为缺少密码的身份生成独立初始密码，已有密码不覆盖。
- 页面使用账号密码登录；本机 `data/auth.accounts.txt` 交付初始密码，alice/bob 为员工、manager 为经理、admin 为管理员。服务端读取 `auth.json` 的密码哈希、身份和部门，不接受前端提交角色。
- 页面使用 HttpOnly、SameSite=Strict 会话 Cookie，最长 8 小时，刷新可恢复，退出后服务端撤销。密码与会话凭据不写入 localStorage/sessionStorage。会话数据库只存 Cookie 的摘要；HTTPS 下 Cookie 带 Secure。
- Cookie 认证写操作要求同源来源及 CSRF 校验，登录有频率限制。CLI 的 API Key 继续保存在 `data/auth.keys.json`，网页登录不使用它。
- 服务间使用另一个随机 Key，保存在 `data/demo_service.key`，不是用户 Key。
- 启动器用子进程环境变量启用 `http_incidents`，不修改用户 `.env`；Ctrl+C 停止它启动的两个子进程。
- 自定义端口：`python -m app.workbench --port 8090 --service-port 8091`。端口已占用会停止启动，不杀死其他进程。

直接执行 `python -m app.api` 仍可打开页面，使用 `.env` 中的动作模式。默认 `simulated` 仅在进程内模拟。已有旧身份配置时先执行 `python -m app.auth init-passwords`。账号文件中禁用用户或更换密码哈希后需重启服务生效，已有会话也会被拒绝；该初始化命令不会重置已有密码。API 仍可供 CLI、测试及其他客户端使用。

网页登录接口为 `POST /auth/login`、`GET /auth/session`、`POST /auth/logout`。前端同源请求带 `X-Portal-Request: 1`，登录/恢复返回身份及 CSRF Token，后续写操作通过 `X-CSRF-Token` 提交。Cookie 由浏览器保存，响应正文不返回登录密码、API Key 或会话 Cookie。初始账号交付文件、认证文件和会话库均不进入版本库。

## 面试演示顺序

1. 用 alice 的账号、密码登录，提交“系统打不开”。按追问补充“ERP 以前正常，现在打开报错超时”。观察同一工单从补充信息到受理的变化。
2. 开启真实模型与调查时，事件先登记，调查在后续执行。员工查看处理进度和待办；经理、管理员在独立的“审批与处理”入口查看排查摘要和依据。诊断假设不能等同于已验证根因或故障修复。
3. 提交“申请报表数据库只读权限”。确认模型提出的具体计划，再切换 manager 审批。申请人不能审批自己的申请。ERP/VPN 只支持普通使用权限，不能申请报表库的只读权限类型。
4. manager 进入“审批与处理”，从“待认领”队列认领工单，再核查证据、请求用户补充、转派或提交处理结果。没有调查报告也可人工处理。建议采纳与问题是否解决分开记录；未解决仍保留处理待办，确认恢复才结束工单。演示输入的反馈不自动成为实验中的真实人工评审证据。
5. 用 bob 的账号、密码登录，不能看到 alice 的工单。手工调用接口也仍受后端权限限制。

## 故障工单的持续处理

登记成功后，故障先排队调查；无法调查或调查结束后进入“待支持核查”，不再以关闭工单代表已解决。支持人员先认领，工单进入“支持处理中”。申请人始终只在自己的工单中查看进度、处理团队及当前负责人，补充信息不改变已授权的账号、资源或动作。

调查缺少发生时间、具体表现、复现步骤或此前使用情况时，可向申请人提出受控问题。内部日志缺失、数据源不可用等问题交给支持人员核查，不要求普通用户提供内部配置。申请人补充后沿用原登记回执启动新一轮调查，旧观察与报告留在调查历史中；默认最多 3 轮调查、2 次自动追问，达到预算后由支持人员继续跟进。即使未收到自动追问，申请人也能为尚未结束的故障补充信息。

支持入口提供“待认领”“我负责的”“已超期”队列。认领与转派校验当前版本，避免多人同时抢占；申请人不能认领自己的申请，转派目标必须是能访问该部门工单的支持人员。只有当前处理人可以请求补充或提交结果，管理员可填写原因后接管，人员变化保留历史。

处理期限按严重程度设置：critical 1 小时、high 4 小时、medium 24 小时、low 72 小时，采用连续计时。等待申请人补充时暂停，收到回复后顺延等待时长。每 30 秒检查超期，写入一次升级记录并在支持队列突出显示；当前提醒范围是站内队列，没有邮件、短信或外部值班平台集成。

`resolved` 表示当前处理人核实问题恢复；`rejected` 表示人工拒绝处理。反馈为未解决或尚未确认时回到可处理状态。结果未知的下游写操作必须先核查并在人工结案中填写凭证，不能通过普通反馈跳过。启动时会将历史未确认解决的关闭故障恢复为支持待办，原回执、报告及审计保留，不重新登记事件。

新增同源入口如下；权限、状态和版本仍由服务端校验，Cookie 写请求继续要求 CSRF：

| 入口 | 作用 |
|---|---|
| `GET /portal/tickets?scope=support&queue=unassigned/mine/overdue` | 读取对应支持队列（queue 选择一个值） |
| `POST /portal/tickets/{id}/claim` | 携带 assignment revision 认领 |
| `POST /portal/tickets/{id}/transfer` | 携带 revision、目标 assignee 和 reason 转派 |
| `POST /portal/tickets/{id}/request-info` | 携带 assignment revision 和受控 field 请求补充 |
| `POST /portal/tickets/{id}/follow-up` | 申请人携带 case revision 和 answer 补充信息 |

CLI 同样提供 `claim`、`transfer`、`request-info`、`follow-up` 子命令及 `--revision` 参数，支持流程仍受同一服务层约束。

## HTTP 契约与结果未知

Agent 的 `IncidentHTTPAdapter` 使用独立 HTTP 服务，并复用原有操作账本：

| 请求 | 作用 | 约束 |
|---|---|---|
| `POST /incidents` | 登记故障事件 | Bearer 服务凭据、固定幂等键、结构化回执 |
| `POST /attachments` | 附加调查证据 | 独立幂等键、必须先存在事件 |
| `GET /operations/{key}` | 只读查询操作回执 | 不重复写入、不自动把 Agent 账本改为成功 |
| `GET /ready` | 检查服务认证和数据库读取 | 无业务写入 |

独立服务把业务对象与幂等回执放在同一个 SQLite 事务中。同键同内容返回已有回执，同键不同内容拒绝；重启后回执仍可查询。

网络超时不等于下游未执行。写入成功而响应丢失时，Agent 保持待核查，不自动重试写操作；经理可从页面读取账本与下游回执，再按实际核查结果人工结案。附件失败不删除已登记事件，也不重新登记。调用方和服务之间不具备分布式事务，不声称全链路 exactly-once。

页面建单使用固定请求标识；受理响应丢失时锁定原内容，重试复用原标识。标识仅在当前页面内存中保存，刷新或切换身份后不保留，因此结果未知时应先查看工单列表再决定后续处理。敏感状态变更不自动重试。

HTTP 连接和失败处理是真实的，但下游是本地独立演示系统，日志与业务对象仍是模拟数据。密码重置、权限授予仍使用明确标注的本地模拟适配器。将来接企业接口还需要按实际契约适配认证、字段、幂等及结果查询，不能只替换 URL 就宣称接入完成。

## 预检与验证证据

```powershell
.\.venv\Scripts\python.exe -m app.preflight --embedding --live --output data/evaluations/my-preflight.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/check_http_release.py --live --output data/evaluations/my-http-release.json
```

预检不写业务数据。`--embedding` 显式检查检索加载，`--live` 调用一次真实模型；没有执行的检查显示 skipped，离线模式显示 warning，不算已验证。独立服务检查依据当前进程配置；启动器注入的配置不会自动写回其他终端。若要在另一个终端检查 HTTP 模式，需设置 `AGENT_ACTION_BACKEND`、`AGENT_ACTION_BASE_URL`、`AGENT_ACTION_API_KEY`，凭据从本机服务 Key 文件读取，不写进脚本。

本轮结果：

- `workbench-preflight-20260929.json`：身份、数据库、12 份规程、真实模型结构化输出、向量加载和独立服务就绪检查。
- `workbench-http-live-20260929.json`：真实 DeepSeek + BGE + 独立 HTTP 服务，VPN / ERP 两条链路；真实调用不代表真实企业数据。
- `workbench-browser-release-20260929/result.json`：浏览器离线业务流程与真实 HTTP 写入、同键重试、身份隔离、取消、报告显示和反馈接口。报告显示使用上条真实模型产物导入的隔离测试夹具；反馈是测试数据，不是人工评测结论。
- `workbench-regression-20260929.log`：本轮全量回归日志。故障测试覆盖并发同键、服务重启、提交后超时、附件失败、错误凭据与异常回执。
- `human-review-pending-20260929.json`：待人工复核状态。人工字段为空时不能计成功；见 `HUMAN_REVIEW.md`。

浏览器验收脚本 `scripts/check_workbench_browser.cjs` 需要单独的 Playwright 开发环境，并指向隔离的测试数据目录。运行工作台本身不需要 Node 或 Playwright。

账号密码登录专项测试：`python -m unittest discover -s tests -p test_password_login.py -v`。`scripts/check_portal_dom.cjs` 使用可选的 jsdom/postcss 开发依赖检查登录、恢复会话、过期返回、角色视图与表单交互，不启动浏览器、不连接真实服务。历史浏览器报告仅证明当时的版本，不能充当新版登录的浏览器验收结果。

外层 LangGraph 与自建 SQLite 业务快照继续保留；本次没有叠加原生 checkpointer、消息队列或多实例服务。业务快照、审计、审批和账本的原有事务边界没有为页面改变。
