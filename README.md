# IT 工单 Agent

一个用 Python 和 LangGraph 开发的 IT 工单项目。用户描述问题后，系统会补问缺失信息、查阅处理规程，再生成处理计划；需要审批的操作会暂停，审批通过后继续执行。遇到故障时，通过 ReAct 调用只读工具查询日志和排障文档，给出带证据来源的诊断建议。

后端使用 FastAPI，附带一个网页工作台。工单状态和操作记录保存在 SQLite 中，权限检查、审批和防止重复执行的逻辑由后端处理。

这是个人开发项目，日志和下游业务使用模拟数据。可以在本地走通处理流程，但不会修改真实账号或开通权限。

## 本地运行

需要 Python 3.11 或以上版本。以下命令在项目根目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m app.auth init
python -m app.workbench --no-browser
```

启动后打开 `http://127.0.0.1:8080`，使用 `data/auth.keys.json` 中生成的身份 Key 登录。该文件不会提交到 Git。

工作台会同时启动一个本地 HTTP 模拟服务（端口 `8091`），用于演示故障登记。按 `Ctrl+C` 关闭两个服务。

完成环境和身份初始化后，也可以运行命令行示例：

```powershell
python run_demo.py
```

## 默认怎么运行

- 默认用离线规则代替模型响应，不需要 API Key，方便先把流程跑起来。
- 诊断采用 ReAct 流程，工具只读取信息，并限制调用次数和耗时。使用真实模型决策需要配置下面的 DeepSeek。
- 排障文档检索配置为 BM25 + BGE 融合召回；未下载 BGE 时，使用词法检索。
- 命令行示例在本地模拟执行；网页工作台通过上述 HTTP 模拟服务登记故障。

## 可选配置

### DeepSeek

在 `.env` 中填写 `DEEPSEEK_API_KEY`，将 `AGENT_LLM_MODE` 改为 `deepseek`。`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL` 按所用 API 服务填写。修改后重启服务。

### BGE 向量检索

```powershell
pip install modelscope
python scripts/download_embedding.py
```

下载后重启服务即可使用。模型保存在 `data/models/`，不会提交到 Git。

### Cross-Encoder 重排

需要对召回结果进一步排序时，下载重排模型并重启服务：

```powershell
python scripts/download_reranker.py
```

没有本地重排模型时，系统保留融合召回的排序结果，并记录未执行重排的原因。

## 目录

```text
app/                 后端代码
app/static/          网页工作台
data/knowledge/      处理规程和排障文档
data/diagnostics/    模拟日志和诊断数据
scripts/             模型下载脚本
```
