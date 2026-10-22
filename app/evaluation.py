"""固定行为回归评测；临时数据库与模拟工具，永不操作现有工单。"""
import argparse
import json
from pathlib import Path
import tempfile
import shutil
import statistics
import time

from .audit import AuditStore
from .auth import Principal
from .config import get_settings
from .knowledge import KnowledgeBase
from .model_adapter import RuleBasedModel, build_model_from_env
from .service import TicketService
from .tools import EnterpriseTools
from .workflow import EnterpriseAgent
from .errors import ConfigurationError


DEFAULT_CASES = Path(__file__).resolve().parents[1] / "evals" / "cases.json"


def evaluate(cases_path=DEFAULT_CASES, mode="offline", *, knowledge_dir=None):
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        overrides = {"llm_mode": "offline", "_env_file": None} if mode == "offline" else {}
        settings = get_settings(data_dir=root, db_path=root / "eval.db", kb_dir=root / "kb", rag_backend="lexical",
                                clarification_max_rounds=3, decision_min_confidence=0.8, **overrides)
        if mode == "configured" and (settings.llm_mode == "offline" or not settings.deepseek_api_key.get_secret_value().strip()):
            raise ConfigurationError("真实评测需要配置真实模型及 API Key；不能用规则结果冒充模型评测")
        source_kb = Path(knowledge_dir) if knowledge_dir else Path(__file__).resolve().parents[1] / "data" / "knowledge"
        if not source_kb.is_dir() or not list(source_kb.rglob("*.md")):
            raise ValueError("评测需要非空 SOP 知识库")
        shutil.copytree(source_kb, root / "kb")
        model = RuleBasedModel() if mode == "offline" else build_model_from_env(settings)
        for case in cases:
            # 每个案例有独立数据库和熔断状态；用例不会互相影响。
            audit = AuditStore(root / (case["id"] + ".db"))
            current_model = model if mode == "offline" else build_model_from_env(settings)
            agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(root / "kb", backend="lexical"), audit),
                                    audit, current_model, settings=settings)
            service = TicketService(agent)
            principal = Principal(user_id="alice", role="employee", department="IT")
            started = time.perf_counter()
            state = None
            confirmations = 0
            try:
                state = service.create(principal, title=case["title"], description=case["description"])
                for answer in case.get("answers", []):
                    if state.ticket.status != "awaiting_clarification":
                        break
                    state = service.clarify(principal, state.ticket.id, answer, state.clarification_round)
                pending = state.confirmation or {}
                expected_plan = case.get("confirm_plan")
                if (state.ticket.status == "awaiting_clarification" and pending.get("status") == "pending"
                        and expected_plan and all(pending["plan"].get(k) == v for k, v in expected_plan.items())):
                    state = service.clarify(principal, state.ticket.id, "确认执行", state.clarification_round)
                    confirmations += 1
                plan_matches = not expected_plan or bool(state.ticket.execution_plan) and all(
                    state.ticket.execution_plan.get(k) == v for k, v in expected_plan.items())
                expected_status = case["status"]
                # Frozen v2 fixtures used closed for completed registration.
                # Preserve those files and explicitly map only that incident
                # contract; actual fault resolution is evaluated separately.
                if expected_status == "closed" and case.get("action") == "open_incident":
                    expected_status = "awaiting_support"
                match = plan_matches and state.ticket.status == expected_status and (not case.get("action") or state.ticket.recommended_action == case["action"])
                executed = state.action_result is not None and state.action_result.status == "succeeded"
                results.append({"id": case["id"], "passed": match, "expected_status": expected_status, "fixture_status": case["status"],
                    "status": state.ticket.status, "action": state.ticket.recommended_action,
                    "incorrect_auto_execution": executed and not match,
                    "clarification_rounds": state.clarification_round,
                    "confirmations": confirmations, "execution_plan": state.ticket.execution_plan,
                    "interpretation_source": (state.clarification or {}).get("source"),
                    "retrieved_sources": sorted({doc["source"] for doc in state.retrieved_docs}),
                    "degraded": bool(state.degradation),
                    "last_planning_usage": (state.decision or {}).get("metadata", {}).get("usage", {}),
                    "decision_source": (state.decision or {}).get("source"),
                    "error": state.error_info.to_dict() if state.error_info else None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
            except Exception as exc:
                results.append({"id": case["id"], "passed": False, "error": type(exc).__name__,
                                "incorrect_auto_execution": None, "clarification_rounds": 0,
                                "note": "流程异常，不能据此认定没有发生执行；必须核查"})
    return {"dataset_version": 2, "case_status_contract": 3, "mode": mode, "configured_llm_mode": settings.llm_mode,
            "model_name": settings.deepseek_model if mode == "configured" else "RuleBasedModel",
            "total": len(results),
            "passed": sum(x["passed"] for x in results),
            "errors": sum(bool(x.get("error")) for x in results),
            "incorrect_auto_executions": sum(x["incorrect_auto_execution"] is True for x in results),
            "unverified_outcomes": sum(x["incorrect_auto_execution"] is None for x in results),
            "median_elapsed_ms": round(statistics.median([x["elapsed_ms"] for x in results if "elapsed_ms" in x]), 2) if any("elapsed_ms" in x for x in results) else None,
            "knowledge_documents": len(list(source_kb.rglob("*.md"))),
            "retrieval_backend": "lexical",
            "cases_with_retrieval": sum(bool(x.get("retrieved_sources")) for x in results),
            "cases_with_model_output": sum(x.get("interpretation_source") == "deepseek" or x.get("decision_source") == "deepseek" for x in results),
            "degraded_cases": sum(bool(x.get("degraded")) for x in results),
            "average_clarification_rounds": round(sum(x["clarification_rounds"] for x in results) / max(len(results), 1), 3),
            "scope": "固定行为回归；使用项目 SOP 的词法检索和模拟工具；确认仅接受案例预设的具体计划；不代表真实工单总体准确率或 BGE 质量", "cases": results}


def main():
    parser = argparse.ArgumentParser(description="Agent 行为回归评测")
    parser.add_argument("--mode", choices=["offline", "configured"], default="offline")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.cases, args.mode)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))
    if report["passed"] != report["total"]:
        print(json.dumps([x for x in report["cases"] if not x["passed"]], ensure_ascii=False, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
