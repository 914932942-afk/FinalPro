<!-- rag: {"kind": "runbook", "document_id": "kb-v02", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-v02", "owner_team": "network-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-v02 排障规程

VPN 排障规程：隧道建立后，如果目标企业网段被本地直连路由覆盖，或预期企业路由未安装，先核查路由表与 split tunneling 配置，交网络支持组。DNS 正常不能排除路由问题。route-nopull 会阻止接受服务端推送的路由等选项；仅见该配置名、但当前路由正常，不构成故障证据。关键词 route routing subnet overlap prefix route-nopull.

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 判定门槛
需要当前路由表、目标网段和客户端 split tunneling 配置；只有日志中出现配置名，不能推断路由已经被覆盖。发现网段冲突时交网络支持组，不能让用户自行删除本地路由。
