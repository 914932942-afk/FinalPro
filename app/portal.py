"""Explicit requester projections and a separately authorized support view.

Display statuses do not mutate workflow snapshots. Model/tool traces are never
included in requester responses.
"""
from .errors import AuthorizationError, ResourceNotFoundError, RequestValidationError
from .case_management import CaseLifecycle
from .models import AgentState
from .case_access import CaseAccess

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
    if status == "manual_review" and (state.get("approval") or {}).get("status") == "invalidated":
        return _status("pending_review", "申请需重新核查", "amber", "processing", "本次审批已失效", "申请身份、审批资格或有效期发生变化，本次申请已停止自动执行。", "请联系 IT 支持人员核查后重新提交申请。", 2)
    if status == "cancelled":
        return _status("cancelled", "已撤销", "neutral", "completed", "工单已撤销", "本次申请已撤销。", "如仍需帮助，可以重新提交一张工单。", 3)
    if status == "rejected" or (status == "resolved_manually" and resolution.get("outcome") == "rejected"):
        return _status("rejected", "未获通过", "neutral", "completed", "本次申请未获通过", "本次申请已结束，未继续执行申请的操作。", "如需了解详情，请联系 IT 支持人员。", 3)
    if status == "awaiting_user_info":
        return _status("needs_input", "待你补充", "amber", "action_required", "排查需要你补充信息", "请回答下方问题，我们会在原工单上继续跟进。", "补充后继续排查，不会重复创建故障事件。", 1)
    if status == "awaiting_verification":
        return _status("awaiting_verification", "待你验证", "amber", "action_required", "请验证处理结果", "支持人员已提交处理结果，仍需要你实际验证是否恢复。", "请选择已恢复，或反馈仍然失败。验证超时会归档为未确认解决。", 3)
    if status == "archived":
        return _status("archived", "已归档 · 未确认解决", "neutral", "completed", "本次处理已归档", "工单处理已归档，尚未确认故障恢复。", "如问题仍存在，可以在有效期内重新打开本工单。", 3)
    if status == "awaiting_clarification":
        confirming = (state.get("confirmation") or {}).get("status") == "pending"
        return _status("confirmation" if confirming else "needs_input", "待你确认" if confirming else "待你补充", "amber", "action_required", "需要你确认申请" if confirming else "补充信息后，我们才能继续处理", "请完成下方事项，我们会继续跟进这张工单。", "核对申请信息并确认。" if confirming else "请在下方补充具体情况。", 1)
    if status == "awaiting_approval":
        return _status("awaiting_approval", "等待审批", "blue", "processing", "申请正在等待审批", "你的申请已提交给有权限的审批人。", "你暂时无需操作，审批结果会在这里更新。", 2)
    if status in {"awaiting_investigation", "investigating"}:
        return _status("investigating", "正在排查", "blue", "processing", "正在排查你的问题", "工单已登记，正在收集相关信息并分析。", "你暂时无需操作，页面会自动更新进度。", 1)
    if status == "resolved" or (status == "resolved_manually" and resolution.get("external_issue_resolved") is not False) or (status == "closed" and resolution.get("external_issue_resolved") is True):
        return _status("resolved", "已解决" if action == "open_incident" else "已完成", "green", "completed", "本次处理已完成", "你已验证并确认问题恢复。" if resolution.get("type") == "user_confirmed" else "本次处理记录已结束。", "如问题再次出现，可在有效期内重新打开；其他新问题请另建工单。", 3)
    if status == "in_progress":
        return _status("in_progress", "支持处理中", "blue", "processing", "支持人员正在处理", "工单已有处理人负责，将继续核查和处理你的问题。", "如有新的报错或线索，可以在下方补充。", 2)
    if status in {"awaiting_support", "manual_review", "failed"} or (status in {"closed", "resolved_manually"} and action == "open_incident"):
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


def plan_rows(plan, binding=None):
    if not plan:
        return []
    rows = [{"label": "申请事项", "value": ACTION_LABELS.get(plan.get("action"), "服务申请")},
            {"label": "业务账号" if binding else "申请账号", "value": (binding.get("account_id") or "待关联") if binding else plan.get("account", "本人账号")},
            {"label": "目标系统", "value": RESOURCE_LABELS.get(plan.get("resource"), "待确认")}]
    if plan.get("permission"):
        rows.append({"label": "权限范围", "value": PERMISSION_LABELS.get(plan["permission"], "待确认")})
    return rows


def diagnostic_data_issue(state):
    from .business_accounts import DATA_GAPS, gap_detail
    ticket = state["ticket"]
    gap = (ticket.get("diagnostic_report") or {}).get("data_availability") or {}
    if gap.get("code") in DATA_GAPS:
        return gap_detail(gap["code"])
    scope = ticket.get("account_binding") or {}
    if scope.get("code") in DATA_GAPS:
        return gap_detail(scope["code"])
    if scope.get("code") == "business_account_disabled":
        return {"code": "business_account_disabled", "message": "业务账号关联记录标记为停用，已交由支持人员核查。",
                "next_check": "请核对业务账号停用原因及实际状态，核验完成前不执行账号变更。"}
    if ticket.get("recommended_action") == "open_incident" and (ticket.get("execution_plan") or {}).get("resource") == "reports_db":
        return gap_detail("diagnosis_not_supported")
    return None


def present_ticket(state, *, summary=False):
    ticket = state["ticket"]
    resolution = state.get("resolution") or {}
    status = status_for(state)
    view = {"id": ticket["id"], "reference": ticket["id"][-8:], "title": ticket["title"],
            "description": ticket["description"], "requester": ticket.get("owner_id"),
            "department": ticket.get("department"), "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"], "severity": SEVERITY_LABELS.get(ticket.get("severity"), "一般"),
            "category": ACTION_LABELS.get(ticket.get("recommended_action"), "服务申请"), "status": status}
    assignment = ticket.get("assignment") or {}
    view["handling"] = {"team": assignment.get("team"), "assignee": assignment.get("assignee"),
                        "due_at": assignment.get("due_at"), "overdue": status["group"] != "completed" and CaseLifecycle.overdue(assignment),
                        "escalated": bool(assignment.get("escalated_at"))}
    waiting = ticket.get("waiting") or {}
    view["waiting"] = {"kind": waiting.get("kind"), "expires_at": waiting.get("expires_at"), "reminded_at": waiting.get("reminded_at")} if waiting else None
    gap = diagnostic_data_issue(state)
    view["data_status"] = {"message": gap["message"], "next_action": "已交由支持人员核查账号关联和日志覆盖，你可以继续补充故障现象。"} if gap else None
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
    activity.extend({"label": row["message"], "at": row["at"]} for row in ticket.get("case_events", []))
    for row in assignment.get("history", []):
        label = "处理人员已调整，正在安排新的负责人。" if row["action"] == "release" else ("工单已由 " if row["action"] == "claim" else "工单已转派给 ") + row["to"] + " 负责"
        activity.append({"label": label, "at": row["at"]})
    if assignment.get("escalated_at"):
        activity.append({"label": "处理超时，已列入经理跟进队列", "at": assignment["escalated_at"]})
    approval = state.get("approval") or {}
    if approval.get("decided_at"):
        activity.append({"label": "审批已通过" if approval["status"] == "approved" else "审批未通过", "at": approval["decided_at"]})
    if ticket["updated_at"] != ticket["created_at"]:
        activity.append({"label": status["label"], "at": ticket["updated_at"]})
    activity.sort(key=lambda row: row["at"])
    if ticket["status"] == "awaiting_user_info":
        question = clarification.get("question", "请补充具体情况。")
    case_open = ticket["status"] in CaseLifecycle.support_states | {"awaiting_user_info"} or (ticket["status"] == "closed" and ticket.get("recommended_action") == "open_incident" and (state.get("resolution") or {}).get("external_issue_resolved") is not True)
    view.update(clarification={"question": question, "round_number": state.get("clarification_round", 0), "stage": "investigation" if ticket["status"] == "awaiting_user_info" else "intake"} if ticket["status"] in {"awaiting_clarification", "awaiting_user_info"} else None,
                confirmation=plan_rows((state.get("confirmation") or {}).get("plan"), ticket.get("account_binding")) if confirming else None,
                request_details=plan_rows(ticket.get("execution_plan"), ticket.get("account_binding")), conversation=history, activity=activity,
                can_cancel=ticket["status"] in {"awaiting_clarification", "awaiting_approval"} or (ticket.get("recommended_action") == "open_incident" and ticket["status"] in CaseLifecycle.open_states),
                can_follow_up=case_open and ticket.get("recommended_action") == "open_incident", case_revision=ticket.get("case_revision", 0),
                verification={"can_confirm": ticket["status"] == "awaiting_verification", "can_reopen": CaseLifecycle.can_reopen_ticket(ticket, resolution),
                              "can_report_recovered": ticket.get("recommended_action") == "open_incident" and ticket["status"] in CaseLifecycle.open_states and ticket["status"] != "awaiting_verification" and (state.get("action_result") or {}).get("status") == "succeeded" and ticket.get("investigation_status") != "attachment_unknown",
                              "reopen_until": resolution.get("reopen_until")},
                needs_polling=ticket["status"] in {"awaiting_investigation", "investigating", "new", "triaged", "planned", "authorized", "executed", "recovering"})
    if (state.get("action_result") or {}).get("status") == "unknown" or ticket.get("investigation_status") == "attachment_unknown":
        view["can_cancel"] = False
    return view


class PortalService:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def check_scope(principal, scope):
        if scope == "support" and principal.role not in {"support", "manager", "admin"}:
            raise AuthorizationError("仅支持人员可以访问处理工作台")

    def get(self, principal, ticket_id, scope="mine"):
        principal = self.service.current(principal)
        self.check_scope(principal, scope)
        state = self.service.get_state(principal, ticket_id)
        if scope == "mine" and state["ticket"].get("owner_id") != principal.user_id:
            raise ResourceNotFoundError()
        view = present_ticket(state)
        if scope == "support":
            view["support"] = self._support(state, principal)
        return view

    def list(self, principal, *, scope="mine", group=None, query="", queue=None, limit=30, offset=0):
        principal = self.service.current(principal)
        self.check_scope(principal, scope)
        if queue and scope != "support":
            raise RequestValidationError()
        rows = []
        for ticket in self.service.agent.audit.list_tickets():
            if scope == "mine" and ticket.get("owner_id") != principal.user_id:
                continue
            try:
                state = self.service.get_state(principal, ticket["id"])
            except ResourceNotFoundError:
                continue
            row = present_ticket(state, summary=True)
            if scope == "support":
                support = self._support(state, principal)
                active = row["status"]["group"] != "completed"
                row["support_queue"] = {"unassigned": support["can_claim"], "mine": active and support["is_assignee"], "overdue": active and row["handling"]["overdue"]}
            rows.append(row)
        counts = {"total": len(rows), "action_required": 0, "processing": 0, "completed": 0}
        for row in rows:
            counts[row["status"]["group"]] += 1
        if scope == "support":
            for key in ("unassigned", "mine", "overdue"):
                counts[key] = sum(bool(row["support_queue"][key]) for row in rows)
        text = query.strip().casefold()
        filtered = [row for row in rows if (not queue or row.get("support_queue", {}).get(queue)) and (not group or row["status"]["group"] == group) and
                    (not text or text in (row["title"] + " " + row["description"] + " " + row["reference"]).casefold())]
        return {"items": filtered[offset:offset + limit], "total": len(filtered), "counts": counts}

    @staticmethod
    def _support(state, principal):
        state = CaseLifecycle.normalize(AgentState.from_dict(state)).to_dict()
        ticket = state["ticket"]
        handling = CaseAccess.internal(principal, ticket)
        report = (ticket.get("diagnostic_report") or {}) if handling else {}
        approval = state.get("approval") or {}
        independent = bool(ticket.get("owner_id")) and ticket["owner_id"] != principal.user_id
        operator = independent and handling
        required_role = approval.get("required_role")
        assignment = ticket.get("assignment") or {}
        job = (ticket.get("investigation_job") or {}) if handling else {}
        job_failed = job.get("status") == "failed"
        is_assignee = operator and assignment.get("assignee") == principal.user_id
        case_active = ticket["status"] in CaseLifecycle.support_states
        can_approve = ticket["status"] == "awaiting_approval" and CaseAccess.can_approve(principal, ticket, required_role)
        observations = [{"source": {"client_events": "客户端记录", "session_events": "关联会话记录", "search_runbooks": "排障规程"}.get(row.get("tool"), "诊断记录"),
                         "records": [{"reference": r.get("id"), "text": r.get("text", "")} for r in row.get("records", [])]}
                        for row in report.get("observations", [])]
        return {"approval_id": approval.get("id"), "can_approve": can_approve,
                "can_claim": operator and ticket["status"] in CaseLifecycle.support_states | {"awaiting_user_info", "awaiting_verification"} and not assignment.get("assignee"),
                "is_assignee": is_assignee, "assignment_revision": assignment.get("revision", 0),
                "can_transfer": operator and bool(assignment.get("assignee")) and (is_assignee or principal.role == "admin") and (case_active or ticket["status"] in {"awaiting_user_info", "awaiting_verification"}),
                "can_request_info": independent and is_assignee and case_active and ticket.get("recommended_action") == "open_incident",
                "assignment_history": assignment.get("history", []) if handling else [],
                "can_investigate": operator and ticket["status"] == "awaiting_investigation",
                "can_feedback": independent and is_assignee and ticket.get("recommended_action") == "open_incident" and ticket["status"] in CaseLifecycle.support_states | {"awaiting_verification"}
                    and (state.get("action_result") or {}).get("status") != "unknown" and ticket.get("investigation_status") != "attachment_unknown",
                "feedback_revision": len(ticket.get("feedback", [])) + 1,
                "can_resolve": independent and is_assignee and ticket["status"] in CaseLifecycle.support_states | {"awaiting_user_info", "awaiting_verification"},
                "can_recover": independent and is_assignee and case_active and state.get("next_node") is not None and not job_failed,
                "can_retry_investigation": independent and is_assignee and case_active and job_failed and state.get("next_node") == "diagnose"
                    and job.get("runs", 0) < job.get("max_runs", 3) and ticket.get("investigation_status") != "attachment_unknown",
                "worker_issue": {"message": {"worker_timeout": "自动排查超过处理时限，已停止等待并交由支持人员跟进。", "queue_timeout": "排查任务未能及时启动，已交由支持人员处理。"}.get(job.get("reason"), "自动排查未能完成，请核查原因后决定是否重新排查。"),
                    "remaining_runs": max(job.get("max_runs", 3) - job.get("runs", 0), 0), "escalated": bool(job.get("exhausted"))} if job_failed else None,
                "summary": report.get("summary"), "next_check": report.get("next_check"),
                "data_issue": diagnostic_data_issue(state) if handling else None, "business_account": (ticket.get("account_binding") or report.get("account_scope") or {}).get("account_id") if handling else None,
                "observations": observations, "citations": report.get("citations", []),
                "feedback": [{"actor": r.get("actor"), "note": r.get("note"), "at": r.get("at")} for r in ticket.get("feedback", [])] if handling else [],
                "diagnostic_note": "未取得该申请人的故障记录，请核查日志来源与对应账号。" if observations and not any(row["records"] for row in observations) else None}
