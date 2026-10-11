<!-- rag: {"kind": "runbook", "document_id": "kb-v06", "resource": "vpn", "service": "remote-access", "version": "demo-v2", "status": "retired", "environment": "any", "family": "vpn-mfa", "owner_team": "identity-support", "severity": "high", "reviewed_at": "2026-09-20", "provenance": "self-authored synthetic IT runbook; not vendor policy"} -->
# VPN 多因素认证挑战排查

## 适用条件、证据与交接
适用于客户端能够访问认证入口，但 MFA challenge 超时、push 未送达或一次性验证码被拒绝的 VPN 请求。需要当前认证 session_ref、challenge_id、认证时间、客户端时钟偏差和 MFA provider 返回的结果码。只看到用户说“验证码不对”时，不足以区分设备时钟、推送延迟和账号策略。

先核对当前 session 的 challenge 状态和时间线，再交身份支持组核查 provider 侧投递记录。不得要求用户提交验证码、恢复码或 token，也不能因为一次 push 超时就重置密码或解除 MFA。

## 边界
此为自编模拟内部运维约定，不是真实企业数据。规程只读；没有当前 challenge_id 时返回证据不足并请求人工补采。
