<!-- rag: {"kind": "runbook", "document_id": "kb-v00", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "active", "environment": "any", "family": "kb-v00", "owner_team": "network-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# kb-v00 排障规程

VPN 故障初步调查：用户仅报告连接失败或超时时，请先取得当前客户端事件和关联会话标识；若隧道状态、解析或路由证据尚未取得，不能确定原因。网络故障处理必须保留时间与会话信息。

## 证据与交接
只采用当前关联会话的证据，保留事件时间、会话标识和原文。历史或其他会话的错误不能证明本次原因。证据不足时说明缺口，交人工补采，不声称已经修复。

## 最低采集字段
客户端版本、发生时间（含时区）、网络接入方式、当前账号、client_event_id 和关联 session_ref。没有 session_ref 时只能登记为待调查事件，不能把本机提示当作根因。

## 升级边界
同一账号在短时间内重复失败，或多个账号同时失败时，交网络支持组核查网关侧事件。此规程只读，不执行重连、改路由或修改终端配置。
