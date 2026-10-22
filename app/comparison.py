"""A/B/C 对照：相同身份、确认、审批与合成案例；不操作真实业务工单。"""
import argparse
import json
from pathlib import Path
import shutil
import statistics
import tempfile
import time

from .audit import AuditStore
from .auth import Principal
from .config import get_settings
from .knowledge import KnowledgeBase
from .model_adapter import RuleBasedModel, build_model_from_env
from .service import TicketService
from .tools import EnterpriseTools
from .workflow import EnterpriseAgent


ROOT = Path(__file__).resolve().parents[1]


def matches(plan, expected):
    return bool(plan and expected) and all(plan.get(k) == v for k, v in expected.items())


def evaluate_variant(cases, variant, *, progress=None):
    records = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        cfg = get_settings(data_dir=root, db_path=root / "eval.db", kb_dir=root / "kb",
            llm_mode="offline" if variant == "A" else "deepseek", llm_strict=True,
            rag_backend="lexical", planning_use_sop=variant == "C", require_action_confirmation=True,
            clarification_max_rounds=3, decision_min_confidence=0.8,
            deepseek_timeout=15, deepseek_max_retries=0)
        shutil.copytree(ROOT / "data" / "knowledge", root / "kb")
        for index, case in enumerate(cases):
            if progress:
                progress(f"{variant} {index + 1}/{len(cases)} {case['id']}")
            audit = AuditStore(root / f"case-{index}.db")
            model = RuleBasedModel() if variant == "A" else build_model_from_env(cfg)
            agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(root / "kb", backend="lexical", settings=cfg), audit),
                                    audit, model, settings=cfg)
            service = TicketService(agent)
            owner = Principal(user_id="alice", role="employee", department="IT")
            manager = Principal(user_id="manager", role="manager", department="IT")
            started = time.perf_counter()
            state = None
            asked, used_answers = [], set()
            confirmation_count = 0
            record = {"id": case["id"], "variant": variant}
            try:
                state = service.create(owner, title=case["title"], description=case["description"])
                for _ in range(4):
                    if state.ticket.status != "awaiting_clarification":
                        break
                    pending = state.confirmation or {}
                    if pending.get("status") == "pending":
                        if not matches(pending.get("plan"), case.get("plan")):
                            break  # 评测用户只确认事先标注的正确计划。
                        reply = "确认执行"
                        confirmation_count += 1
                    else:
                        fields = state.clarification["missing_fields"]
                        asked.extend(fields)
                        available = [field for field in fields if field in case.get("answers", {}) and field not in used_answers]
                        if not available:
                            break
                        field = available[0]
                        reply = case["answers"][field]
                        used_answers.add(field)
                    state = service.clarify(owner, state.ticket.id, reply, state.clarification_round)
                if state.ticket.status == "awaiting_approval" and matches(state.ticket.execution_plan, case.get("plan")):
                    state = service.approve(manager, state.approval.id, True)
                correct_plan = not case.get("plan") or matches(state.ticket.execution_plan, case["plan"])
                right_questions = all(field in asked for field in case.get("required_questions", []))
                passed = state.ticket.status == case["status"] and correct_plan and right_questions
                succeeded = bool(state.action_result and state.action_result.status == "succeeded")
                events = audit.list_events(state.ticket.id)
                calls = []
                for event in events:
                    output = (event["payload"].get("clarification") if event["node"] == "clarify" else
                              event["payload"].get("decision") if event["node"] == "plan" else None)
                    if output and output.get("source") == "deepseek":
                        calls.append(output.get("metadata", {}))
                sources = {doc["id"]: doc.get("source") for doc in state.retrieved_docs}
                refs = (state.planning or {}).get("sop_refs", [])
                record.update(passed=passed, status=state.ticket.status, execution_plan=state.ticket.execution_plan,
                    incorrect_execution=succeeded and (not correct_plan or case["status"] != "closed" or not right_questions),
                    asked_fields=asked, unnecessary_questions=[f for f in asked if f not in case.get("answers", {})] if case.get("plan") else [],
                    confirmations=confirmation_count, llm_successful_calls=len(calls),
                    token_usage={key: sum(call.get("usage", {}).get(key, 0) for call in calls)
                                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    sop_sources=[sources[ref] for ref in refs if ref in sources],
                    decision=state.planning, error=state.error_info.to_dict() if state.error_info else None,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            except Exception as exc:
                record.update(passed=False, error=type(exc).__name__, incorrect_execution=None,
                              elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            records.append(record)
    return {"variant": variant, "total": len(records), "passed": sum(x["passed"] for x in records),
        "incorrect_executions": sum(x.get("incorrect_execution") is True for x in records),
        "unverified_outcomes": sum(x.get("incorrect_execution") is None for x in records),
        "failed_cases": sum(bool(x.get("error")) for x in records),
        "llm_successful_calls": sum(x.get("llm_successful_calls", 0) for x in records),
        "total_tokens": sum(x.get("token_usage", {}).get("total_tokens", 0) for x in records),
        "unnecessary_questions": sum(len(x.get("unnecessary_questions", [])) for x in records),
        "median_elapsed_ms": round(statistics.median(x["elapsed_ms"] for x in records), 2), "cases": records}


def main():
    parser = argparse.ArgumentParser(description="受控 A/B/C 对照评测；B/C 会调用真实模型")
    parser.add_argument("--variants", choices=["A", "B", "C", "ABC"], default="A")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "decision_comparison.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    reports = []
    for variant in args.variants:
        reports.append(evaluate_variant(cases, variant, progress=lambda message: print(message, flush=True)))
        report = {"dataset": str(args.cases.name), "scope": "合成开发案例；模拟下游；词法检索；非独立盲测，不代表总体准确率；token 仅统计成功保存的模型响应",
            "controls": "相同身份、资源目录、本人确认、审批、轮次和预标注用户回答；B/C 相同模型与参数", "results": reports}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "cases"} for r in reports], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
