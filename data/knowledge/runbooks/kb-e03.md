<!-- rag: {"kind": "runbook", "document_id": "kb-e03", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-e03", "owner_team": "database-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-e03 排障规程

ERP 排障规程：同一请求的数据库事务出现循环等待并被回滚，属于需要应用与数据库支持共同核查的事务锁竞争线索。应附会话标识、被回滚事务及锁等待关系，核查事务访问顺序与重试策略。单独出现 lock 或历史死锁告警不足以支持当前事务判断，不能自动重放用户写操作。关键词 deadlock transaction circular wait rollback lock cycle victim.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
需要当前事务标识、锁等待关系和回滚记录；单独出现 lock、deadlock 或历史告警不足以证明当前请求受影响。只读调查不得自动重放用户写操作。
