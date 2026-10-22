# 冻结与人工复核

本轮提供可执行的冻结、方案标签隐藏、人工评分流程，没有生成或冒充独立人工结论。

现有 `data/evaluations/human-review-freeze-20260929.json` 是对已完成的 v3 自编验证集做的复核归档，**不是重新声称它在历史实验前由独立人员冻结**。它明确保留作者来源。`human-review-20260929/review-packet.json` 包含 60 条待复核答案，人工字段全部留空；`private-mapping.json` 由组织评测的人保管，不交给评审人。

给评审人复制 review-packet.json 为 reviewed.json，只填写 human_review。分别判断证据支持、规程适用、交接建议有效性、不确定性，并填写姓名和原因。生成缺失不会被算作成功；评审缺失保持未完成。评审器拒绝修改原答案或原证据。这里只隐藏方案标签与模型评分，候选文档内容仍可能让评审人推测方案，不能声称严格双盲。

```powershell
.\.venv\Scripts\python.exe -m app.human_review score --packet data/evaluations/human-review-20260929/review-packet.json --reviewed reviewed.json --mapping data/evaluations/human-review-20260929/private-mapping.json --output data/evaluations/my-human-score.json
```

后续独立数据由其他人根据 `evals/independent_review_template.json` 填写。至少包含口语改写、环境未知或冲突、历史错误已恢复、无适用答案；不为了保证召回而复制规程关键词。先人工锁定标签，再冻结文件和知识库摘要，之后才运行实验。修改规程或标签应产生新版本，不能覆盖旧结果。

```powershell
.\.venv\Scripts\python.exe -m app.human_review freeze --fixture independent-cases.json --author reviewer-name --independent-author-declared --output data/evaluations/new-frozen.json
.\.venv\Scripts\python.exe -m app.knowledge_experiment --live --fixture independent-cases.json --variants bm25 vector full_context --output data/evaluations/new-live.json
.\.venv\Scripts\python.exe -m app.human_review packet --frozen data/evaluations/new-frozen.json --evaluation data/evaluations/new-live.json --output-dir data/evaluations/new-review
```

`--independent-author-declared` 只是来源声明，不是程序已经证明独立。完成同模型评审、独立人员复核和实际使用反馈是三个不同层次，不能互相替代。统计时保留样本数、拒答与错误建议，不把空评审计为通过。
