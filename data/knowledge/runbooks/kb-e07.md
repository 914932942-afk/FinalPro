<!-- rag: {"kind": "runbook", "document_id": "kb-e07", "resource": "erp", "service": "business-application", "version": "demo-v2", "status": "retired", "environment": "any", "family": "erp-authorization", "owner_team": "identity-support", "severity": "medium", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# ERP 已有访问突然返回 403 排查

## 适用条件、证据与交接
适用于用户明确说明此前可以访问同一 ERP 资源，但当前请求返回 403。需要当前资源标识、账号、授权服务 decision、策略版本和 session_ref；首次申请访问不适用本规程，应回到权限申请流程。

先核对授权服务是否读到当前账号、部门和资源绑定，再交身份支持组核查策略缓存和最近变更。不得把 403 自动转换为权限申请，也不能由模型修改角色或绕过审批。

## 边界
此为自编模拟内部运维约定，不是真实企业数据。没有“此前可访问”的用户证据或授权 decision 时，只能澄清或人工核查。

