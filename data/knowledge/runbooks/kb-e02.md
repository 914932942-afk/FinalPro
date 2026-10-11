<!-- rag: {"kind": "runbook", "document_id": "kb-e02", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-e02", "owner_team": "identity-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-e02 排障规程

ERP 登录排障规程：签名验证成功不代表令牌可用于当前应用。若当前请求的 token audience 与 ERP 服务期望值不同，交身份集成支持核查应用注册、环境及回调配置。密码重置不能纠正 audience 不匹配；若 audience 验证已通过，不要套用该规程。关键词 token audience aud relying party recipient signature login 401.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
需要当前请求的 audience、ERP 期望 audience、issuer 和关联 session_ref。签名验证成功只能说明令牌完整性通过；不能由此推断密码错误，也不能要求用户提交令牌正文。
