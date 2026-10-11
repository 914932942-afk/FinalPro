<!-- rag: {"kind": "runbook", "document_id": "kb-v07", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "retired", "environment": "any", "family": "vpn-gateway", "owner_team": "network-support", "severity": "critical", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# VPN 网关服务不可用排查

## 适用条件、证据与交接
适用于多个账号在同一时间窗口出现网关 503、connection refused 或维护窗口响应，且客户端没有建立隧道的情况。需要当前网关地址、事件时间、受影响账号数量、client_event_id 及服务端入口日志。单个用户的连接超时不能证明网关整体不可用。

当多个账号的当前 session 都在入口失败时，登记高优先级事件并交网络平台组核查网关健康、发布窗口和入口负载。不得自动重启网关、修改路由或向用户承诺恢复时间。

## 边界
此为自编模拟内部运维约定，不是真实企业数据。没有同时间窗口的多用户证据时，按普通 VPN 调查处理。
