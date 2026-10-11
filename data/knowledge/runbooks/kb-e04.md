<!-- rag: {"kind": "runbook", "document_id": "kb-e04", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "active", "environment": "cloud-proxy", "family": "erp-pool-managed", "owner_team": "platform-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic deployment policy; not a vendor definition"} -->
# 云连接代理租约排查

## 适用条件、证据与交接
适用于日志明确标记 deployment=cloud-proxy 的 ERP，且连接池等待超时、同一时间数据库健康探针成功。该演示环境通过托管连接代理分配租约；应用连接池扩容不解决租约上限。先向平台连接代理组提交租约分配指标、当前 request/session 和代理实例标识，核查租约排队时间。交接单必须包含租约分配指标；不得仅凭健康探针正常排除全部数据库问题，也不得自动扩容。

## 边界
此为模拟内部运维约定，不是真实企业数据。未取得部署标识时，先核实环境，不能猜测。所有结论均为待验证假设；本模块只读。
