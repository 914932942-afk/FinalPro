<!-- rag: {"kind": "runbook", "document_id": "kb-v05", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "self-managed", "family": "vpn-dns-managed", "owner_team": "network-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic deployment policy; not a vendor definition"} -->
# 自管终端域名后缀排查

## 适用条件、证据与交接
适用于日志明确标记 endpoint=self-managed 的 VPN，隧道建立、目标 IP 可达但企业域名解析失败。该演示环境允许支持人员检查客户端连接配置，向网络支持组提交域名后缀列表与解析结果，核查后缀匹配。交接必须包含域名后缀列表；终端管理平台不负责该设备，不应让用户提供集中下发策略修订号。

## 边界
此为模拟内部运维约定，不是真实企业数据。未取得部署标识时，先核实环境，不能猜测。所有结论均为待验证假设；本模块只读。
