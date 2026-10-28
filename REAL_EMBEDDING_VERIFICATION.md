# BGE + Chroma 真实验证记录

> 历史组件验证：本文保留 2026-09-18 的 BGE 编码与 Chroma 实验结果，不是当前诊断检索架构说明。当前诊断使用适用范围过滤后的 BGE 向量排序，不依赖诊断 Chroma 索引；当前运行配置与加载策略见 [README](README.md)。下文路径、加载行为和命令对应当时的验证版本。

验证日期：2026-09-18

下载来源：ModelScope `AI-ModelScope/bge-small-zh-v1.5`。Hugging Face 的配置文件可以访问，但权重会跳转到 `us.aws.cdn.hf.co`，在当前网络下读取超时；ModelScope CDN 支持稳定的分块下载。

本地目录：`data/models/bge-small-zh-v1.5`

模型加载与编码结果：

```text
SentenceTransformer(data/models/bge-small-zh-v1.5, device=cpu)
shape = (3, 512)
normalized vector norms = [1.0, 1.0, 1.0]
```

真实 Chroma 建索引与检索：

```text
reindex = {'chunks': 3, 'backend': 'chroma'}
忘记密码怎么重置       -> password reset       score=0.7177
如何申请生产库权限     -> access request       score=0.6642
线上服务宕机怎么办     -> incident response    score=0.4697
collection.count() = 3
```

`model.safetensors` SHA-256：`354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026`（95,827,648 bytes）。

重复启动时 Chroma 从持久化目录恢复，查询仍返回同一来源；`KnowledgeBase` 会优先使用本地模型目录。若删除该目录且未设置 `AGENT_EMBEDDING_MODEL`，才会尝试从 Hugging Face 下载模型。

复现：

```powershell
cd C:\Users\gjj33\Desktop\qz\pro
$env:AGENT_RAG_BACKEND = "chroma"
python -m app.cli index
python -X utf8 -m unittest discover -s tests -v
```
