<!-- rag: {"kind": "runbook", "document_id": "kb-e01", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-e01", "owner_team": "application-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-e01 排障规程

ERP 排障规程：浏览器 504 或请求超时并不足以判定数据库慢。若对应服务端请求等待连接池空闲连接而超时，且同时间数据库健康查询迅速完成，优先由应用运维核查连接释放、连接池上限与请求并发；不能据此执行扩容或杀进程。关键词 pool connection wait acquire checkout timeout database health.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
需要同一请求的连接池等待证据和同时间窗口的数据库健康结果。健康查询迅速完成只能缩小假设，不能证明所有业务查询正常；禁止自动扩容、杀进程或重放写请求。
