<!-- rag: {"kind": "runbook", "document_id": "kb-e00", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-e00", "owner_team": "application-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-e00 排障规程

ERP 故障初步调查：系统打不开、报错、504 或 401 只是表象。需要关联当前请求 trace 或 session 的应用日志和支持规程；证据不足时保留未知并请求人工补采日志。

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 受理边界
先确认请求资源、时间窗口和当前账号；没有当前 trace/session 关联时，只能创建故障事件。不得把浏览器状态码直接映射为数据库、权限或网络根因。
