<!-- rag: {"kind": "runbook", "document_id": "kb-v04", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "managed-terminal", "family": "vpn-dns-managed", "owner_team": "endpoint-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic deployment policy; not a vendor definition"} -->
# 托管终端域名策略排查

## 适用条件、证据与交接
适用于日志明确标记 endpoint=managed-terminal 的 VPN，隧道建立、目标 IP 可达但企业域名解析失败。该演示环境的域名策略由终端管理平台集中下发；先由终端策略组核查策略修订号和最近下发结果。交接必须包含策略修订号；不得建议员工手动覆盖 DNS，也不能因解析失败直接申请权限。

## 边界
此为模拟内部运维约定，不是真实企业数据。未取得部署标识时，先核实环境，不能猜测。所有结论均为待验证假设；本模块只读。
