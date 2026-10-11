<!-- rag: {"kind": "runbook", "document_id": "kb-e05", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "classic-direct", "family": "erp-pool-managed", "owner_team": "application-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic deployment policy; not a vendor definition"} -->
# 直连部署连接回收排查

## 适用条件、证据与交接
适用于日志明确标记 deployment=classic-direct 的 ERP，且连接池等待超时、数据库健康探针成功。该演示环境直接连接数据库，不使用托管连接代理。向应用运维组提供连接借还计数、请求并发和当前 session，先核查连接回收。交接单必须包含连接借还计数；不要要求采集代理租约，也不能自动扩容或杀进程。

## 边界
此为模拟内部运维约定，不是真实企业数据。未取得部署标识时，先核实环境，不能猜测。所有结论均为待验证假设；本模块只读。
