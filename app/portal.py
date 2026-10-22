"""Explicit requester projections and a separately authorized support view.

Display statuses do not mutate workflow snapshots. Model/tool traces are never
included in requester responses.
"""
from .errors import AuthorizationError, ResourceNotFoundError

RESOURCE_LABELS = {"erp": "ERP", "vpn": "VPN", "reports_db": "报表数据库", "self_account": "本人账号"}
ACTION_LABELS = {"reset_password": "密码重置申请", "grant_access": "访问权限申请", "open_incident": "故障报修"}
PERMISSION_LABELS = {"read_only": "只读权限", "use": "普通使用权限"}
SEVERITY_LABELS = {"low": "较低", "medium": "一般", "high": "较高", "critical": "紧急"}


def _status(code, label, tone, group, title, message, next_action, stage):
    return dict(code=code, label=label, tone=tone, group=group, title=title, message=message, next_action=next_action, stage=stage)


def status_for(state):
    ticket = state["ticket"]
    status = ticket["status"]
    report = ticket.get("diagnostic_report") or {}
    resolution = state.get("resolution") or {}
    action = ticket.get("recommended_action")
    if status == "cancelled":
        return _status("cancelled", "已撤销", "neutral", "completed", "工单已撤销", "本次申请已撤销。", "如仍需帮助，可以重新提交一张工单。", 3)
    if status == "rejected" or (status == "resolved_manually" and resolution.get("outcome") == "rejected"):
        return _status("rejected", "未获通过", "neutral", "completed", "本次申请未获通过", "本次申请已结束，未继续执行申请的操作。", "如需了解详情，请联系 IT 支持人员。", 3)
    if status == "awaiting_clarification":
        confirming = (state.get("confirmation") or {}).get("status") == "pending"
        return _status("confirmation" if confirming else "needs_input", "待你确认" if confirming else "待你补充", "amber", "action_required", "需要你确认申请" if confirming else "补充信息后，我们才能继续处理", "请完成下方事项，我们会继续跟进这张工单。", "核对申请信息并确认。" if confirming else "请在下方补充具体情况。", 1)
    if status == "awaiting_approval":
        return _status("awaiting_approval", "等待审批", "blue", "processing", "申请正在等待审批", "你的申请已提交给有权限的审批人。", "你暂时无需操作，审批结果会在这里更新。", 2)
    if status in {"awaiting_investigation", "investigating"}:
        return _status("investigating", "正在排查", "blue", "processing", "正在排查你的问题", "工单已登记，正在收集相关信息并分析。", "你暂时无需操作，页面会自动更新进度。", 1)
    if (status == "resolved_manually" and resolution.get("external_issue_resolved") is not False) or (status == "closed" and resolution.get("external_issue_resolved") is True):
        return _status("resolved", "已解决" if action == "open_incident" else "已完成", "green", "completed", "本次处理已完成", "支持人员已确认完成本次处理。", "如有新的问题，可以重新提交工单。", 3)
    if status in {"manual_review", "failed"} or (status in {"closed", "resolved_manually"} and action == "open_incident"):
        supported = report.get("assessment") == "supported_hypothesis" or report.get("verified") is True
        message = "初步排查已完成，结论仍需要支持人员核实。" if supported else "自动排查暂未形成可用结论，需要支持人员进一步核查。目前尚未确认问题已解决。"
        if resolution.get("external_issue_resolved") is False:
            message = "支持人员已核查，问题目前尚未解决，需要进一步处理。"
        return _status("pending_review", "待支持核查", "amber", "processing", "需要支持人员进一步核查", message, "请保留相关报错和发生时间，方便支持人员跟进。", 2)
    if status == "closed":
        message = "密码重置任务已创建。" if action == "reset_password" else "本次申请已处理完成。"
        if (state.get("action_result") or {}).get("simulated"):
            message = "演示处理已完成，本地环境不会修改真实账号或权限。"
        return _status("fulfilled", "申请已处理", "green", "completed", "本次申请已处理", message, "如仍需帮助，可以重新提交工单。", 3)
    return _status("processing", "处理中", "blue", "processing", "已收到，正在处理", "我们已收到你的申请，正在确认处理方式。", "后续进展会在这里更新。", 1)


def plan_rows(plan):
    if not plan:
        return []
    rows = [{"label": "申请事项", "value": ACTION_LABELS.get(plan.get("action"), "服务申请")},
            {"label": "申请账号", "value": plan.get("account", "本人账号")},
            {"label": "目标系统", "value": RESOURCE_LABELS.get(plan.get("resource"), "待确认")}]
    if plan.get("permission"):
        rows.append({"label": "权限范围", "value": PERMISSION_LABELS.get(plan["permission"], "待确认")})
    return rows


def present_ticket(state, *, summary=False):
    ticket = state["ticket"]
    status = status_for(state)
    view = {"id": ticket["id"], "reference": ticket["id"][-8:], "title": ticket["title"],
            "description": ticket["description"], "requester": ticket.get("owner_id"),
            "department": ticket.get("department"), "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"], "severity": SEVERITY_LABELS.get(ticket.get("severity"), "一般"),
            "category": ACTION_LABELS.get(ticket.get("recommended_action"), "服务申请"), "status": status}
    if summary:
        return view
    clarification = state.get("clarification") or {}
    confirming = (state.get("confirmation") or {}).get("status") == "pending"
    questions = {
        "intent": "请说明你遇到的问题，或希望申请的服务。",
        "target": "请说明需要处理的是 ERP、VPN，还是报表数据库。",
        "symptom": "请补充具体表现、报错内容和发生时间，例如卡在登录页还是进入系统后无法打开页面。",
        "previous_access": "之前是否可以正常使用，还是第一次使用这个系统？",
        "requester": "请补充本次申请人的信息。请勿提供密码或验证码。",
        "department": "请补充所属部门。",
        "permission": "请说明需要的访问权限：ERP 或 VPN 的普通使用权限，或报表数据库的只读权限。",
        "single_intent": "一张工单处理一个问题，请说明本次最需要处理的事项。",
        "single_resource": "请说明本张工单需要处理的一个系统。",
        "confirmation": "请明确本次希望办理的事项；如不再需要，可以撤销工单。",
    }
    question = "请核对以下申请信息，确认后我们将继续处理。" if confirming else " ".join(questions.get(field, "") for field in clarification.get("missing_fields", [])).strip()
    if not question:
        question = clarification.get("question", "请补充具体情况。")
    history = [{"role": row["role"], "message": "已确认本次申请。" if row.get("kind") == "plan_confirmation" else row.get("content", ""),
                "at": row.get("at")} for row in state.get("conversation_history", []) if row.get("role") in {"assistant", "user"}]
    activity = [{"label": "工单已提交", "at": ticket["created_at"]}]
    approval = state.get("approval") or {}
    if approval.get("decided_at"):
        activity.append({"label": "审批已通过" if approval["status"] == "approved" else "审批未通过", "at": approval["decided_at"]})
    if ticket["updated_at"] != ticket["created_at"]:
        activity.append({"label": status["label"], "at": ticket["updated_at"]})
    view.update(clarification={"question": question, "round_number": state.get("clarification_round", 0)} if ticket["status"] == "awaiting_clarification" else None,
                confirmation=plan_rows((state.get("confirmation") or {}).get("plan")) if confirming else None,
                request_details=plan_rows(ticket.get("execution_plan")), conversation=history, activity=activity,
                can_cancel=ticket["status"] in {"awaiting_clarification", "awaiting_approval"},
                needs_polling=ticket["status"] in {"awaiting_investigation", "investigating", "new", "triaged", "planned", "authorized", "executed", "recovering"})
    return view


class PortalService:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def check_scope(principal, scope):
        if scope == "support" and principal.role not in {"manager", "admin"}:
            raise AuthorizationError("仅支持人员可以访问处理工作台")

    def get(self, principal, ticket_id, scope="mine"):
        self.check_scope(principal, scope)
        state = self.service.get_state(principal, ticket_id)
        if scope == "mine" and state["ticket"].get("owner_id") != principal.user_id:
            raise ResourceNotFoundError()
        view = present_ticket(state)
        if scope == "support":
            view["support"] = self._support(state, principal)
        return view

    def list(self, principal, *, scope="mine", group=None, query="", limit=30, offset=0):
        self.check_scope(principal, scope)
        rows = []
        for ticket in self.service.agent.audit.list_tickets():
            if scope == "mine" and ticket.get("owner_id") != principal.user_id:
                continue
            try:
                state = self.service.get_state(principal, ticket["id"])
            except ResourceNotFoundError:
                continue
            rows.append(present_ticket(state, summary=True))
        counts = {"total": len(rows), "action_required": 0, "processing": 0, "completed": 0}
        for row in rows:
            counts[row["status"]["group"]] += 1
        text = query.strip().casefold()
        filtered = [row for row in rows if (not group or row["status"]["group"] == group) and
                    (not text or text in (row["title"] + " " + row["description"] + " " + row["reference"]).casefold())]
        return {"items": filtered[offset:offset + limit], "total": len(filtered), "counts": counts}

    @staticmethod
    def _support(state, principal):
        ticket = state["ticket"]
        report = ticket.get("diagnostic_report") or {}
        approval = state.get("approval") or {}
        independent = bool(ticket.get("owner_id")) and ticket["owner_id"] != principal.user_id
        required_role = approval.get("required_role")
        can_approve = independent and ticket["status"] == "awaiting_approval" and required_role in {"manager", "admin"} and (required_role == "manager" or principal.role == "admin")
        observations = [{"source": {"client_events": "客户端记录", "session_events": "关联会话记录", "search_runbooks": "排障规程"}.get(row.get("tool"), "诊断记录"),
                         "records": [{"reference": r.get("id"), "text": r.get("text", "")} for r in row.get("records", [])]}
                        for row in report.get("observations", [])]
        return {"approval_id": approval.get("id"), "can_approve": can_approve,
                "can_investigate": ticket["status"] == "awaiting_investigation",
                "can_feedback": independent and bool(report) and ticket["status"] in {"closed", "resolved_manually"},
                "feedback_revision": len(ticket.get("feedback", [])) + 1,
                "can_resolve": independent and ticket["status"] in {"manual_review", "failed"},
                "can_recover": ticket["status"] in {"manual_review", "failed"},
                "summary": report.get("summary"), "next_check": report.get("next_check"),
                "observations": observations, "citations": report.get("citations", []),
                "feedback": [{"actor": r.get("actor"), "note": r.get("note"), "at": r.get("at")} for r in ticket.get("feedback", [])],
                "diagnostic_note": "未取得该申请人的故障记录，请核查日志来源与对应账号。" if observations and not any(row["records"] for row in observations) else None}
