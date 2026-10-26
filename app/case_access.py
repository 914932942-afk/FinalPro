"""Server-owned support routing, separate from departmental business approval."""
GROUPS = {"network": "网络支持", "applications": "应用支持", "data": "数据服务支持", "service_desk": "IT 服务支持"}
RESOURCE_GROUPS = {"vpn": "network", "erp": "applications", "reports_db": "data"}


class CaseAccess:
    @staticmethod
    def group(ticket):
        return RESOURCE_GROUPS.get((ticket.get("execution_plan") or {}).get("resource"), "service_desk")

    @classmethod
    def can_handle(cls, principal, ticket):
        return (principal.role == "admin" or principal.role in {"support", "manager"}
                and cls.group(ticket) in principal.support_groups)

    @staticmethod
    def can_approve(principal, ticket, required_role="manager"):
        return (bool(ticket.get("owner_id")) and ticket["owner_id"] != principal.user_id
                and required_role in {"manager", "admin"}
                and (principal.role == "admin" or required_role == "manager"
                     and principal.role == "manager" and principal.department == ticket["department"]))

    @classmethod
    def can_read(cls, principal, ticket):
        return (principal.role == "admin" or bool(ticket.get("owner_id")) and
                (ticket["owner_id"] == principal.user_id or cls.can_handle(principal, ticket)
                 or ticket.get("recommended_action") == "grant_access"
                 and principal.role == "manager" and principal.department == ticket["department"]))

    @classmethod
    def internal(cls, principal, ticket):
        return ticket.get("owner_id") != principal.user_id and cls.can_handle(principal, ticket)
