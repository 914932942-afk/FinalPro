<!-- rag: {"kind": "runbook", "document_id": "kb-v03", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-v03", "owner_team": "endpoint-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-v03 排障规程

VPN 排障规程：TLS 连接需检查证书验证记录。若当前验证时刻超出证书 notBefore/notAfter 有效区间，先核对客户端时钟及证书更新状态，交终端支持；不得直接断定密码错误，也不应自动更换证书。历史证书报错若已更新成功，不能解释新的网络故障。关键词 TLS certificate validity expired notAfter notBefore verify.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
必须引用当前连接的证书校验时间、notBefore/notAfter 和客户端时钟；证书已经更新且当前校验通过时，旧错误不能作为本次结论。涉及证书替换时转人工处理。
