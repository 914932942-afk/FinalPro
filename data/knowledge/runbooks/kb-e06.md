<!-- rag: {"kind": "runbook", "document_id": "kb-e06", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "retired", "environment": "any", "family": "erp-service-availability", "owner_team": "application-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# ERP 服务入口 503 排查

## 适用条件、证据与交接
适用于当前 ERP 请求明确返回 503 或 upstream unavailable，且同一时间窗口存在服务入口健康检查失败。需要 request_id、入口实例、响应时间、发布或维护标记和关联 session_ref。浏览器显示 503 但入口健康检查正常时，不能直接归因于服务整体不可用。

先关联当前请求与入口日志，确认是否为同一实例或同一发布窗口；若多个用户同时受影响，交应用平台组核查发布、负载和依赖服务。不得自动回滚发布、重启实例或重放用户写请求。

## 边界
此为自编模拟内部运维约定，不是真实企业数据。缺少 request_id 或当前时间窗口时，返回证据不足并请求人工补采。
