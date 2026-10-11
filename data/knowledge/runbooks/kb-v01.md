<!-- rag: {"kind": "runbook", "document_id": "kb-v01", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-v01", "owner_team": "network-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-v01 排障规程

VPN 排障规程：隧道已建立，企业服务的 IP 地址可达，而同一服务的域名解析失败时，应检查 VPN 下发的 DNS 与域名后缀设置。不能仅凭连接超时认定 DNS 故障。向网络支持组附上当前会话的解析查询结果及直接 IP 连通性证据。关键词 DNS NXDOMAIN name resolution resolver.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
必须同时看到隧道已建立、目标 IP 可达和域名解析失败；只出现 NXDOMAIN 或用户说“打不开”时，先补采当前解析结果和 session_ref。解析恢复后，不继续沿用历史 DNS 规程。
