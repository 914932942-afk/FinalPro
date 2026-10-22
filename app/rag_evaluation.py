"""可复现的 RAG 受控实验：固定输入、真实 BGE/DeepSeek、预标注评分，无业务副作用。"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import time

from .audit import AuditStore
from .auth import Principal
from .config import get_settings
from .knowledge import KnowledgeBase
from .model_adapter import DeepSeekDecisionModel
from .models import AgentState, Ticket
from .planning import validate_choice
from .policy import analyze_request
from .service import TicketService
from .tools import EnterpriseTools
from .workflow import EnterpriseAgent


ROOT = Path(__file__).resolve().parents[1]


def route_matches(choice, expected):
    if choice.next_step != expected["next_step"]:
        return False
    if choice.next_step == "propose_action":
        return all(getattr(choice, key) == expected[key] for key in ("action", "resource", "permission"))
    if choice.next_step == "clarify":
        return choice.missing_fact == expected["missing_fact"]
    return True


def score_decision(choice, expected, *, valid=True, context_appropriate=True, threshold=.8):
    """分别记录模型原始分支和现有工作流置信度门槛后的分支，避免混淆。"""
    raw_correct = route_matches(choice, expected)
    effective = choice
    if choice.next_step == "propose_action" and choice.confidence < threshold:
        effective = choice.model_copy(update={"next_step": "manual_review", "action": None,
                                              "permission": None, "missing_fact": None})
    effective_correct = valid and route_matches(effective, expected)
    return dict(raw_route_correct=raw_correct, raw_valid_correct=raw_correct and valid,
                effective_next_step=effective.next_step if valid else "failed",
                effective_route_correct=effective_correct,
                passed=effective_correct and context_appropriate)


class RagExperiment:
    def __init__(self, directory, *, live=False):
        self.root = Path(directory)
        self.cfg = get_settings(data_dir=self.root, db_path=self.root / "eval.db", kb_dir=self.root / "kb",
            chroma_dir=self.root / "chroma", rag_backend="chroma", rag_hybrid=True,
            embedding_model=str(ROOT / "data/models/bge-small-zh-v1.5"),
            llm_mode="deepseek" if live else "offline", llm_strict=True, deepseek_temperature=0,
            deepseek_timeout=30, deepseek_max_retries=0, require_action_confirmation=True)
        shutil.copytree(ROOT / "data/knowledge", self.cfg.kb_dir)
        self.kb = KnowledgeBase(self.cfg.kb_dir, settings=self.cfg)
        self.kb.reindex()  # 必须真实向量索引成功，不允许悄悄降级并声称使用 BGE。
        self.model = None
        if live:
            self.model = DeepSeekDecisionModel(self.cfg.deepseek_api_key.get_secret_value(),
                base_url=self.cfg.deepseek_base_url, model=self.cfg.deepseek_model,
                timeout=self.cfg.deepseek_timeout, max_retries=0, temperature=0)

    def close(self):
        if getattr(self.kb, "client", None):
            self.kb.client._system.stop()

    @staticmethod
    def ticket(case):
        return Ticket(case["title"], case["description"], "alice", "IT", owner_id="alice")

    def retrieve(self, text, variant):
        if variant == "no_rag":
            return [], {"backend": "none"}
        self.kb.hybrid = variant == "hybrid"
        docs, status = self.kb.search_with_status(text, 3)
        if status.get("degraded"):
            raise RuntimeError("real BGE experiment cannot silently use fallback")
        return docs, status

    def run(self, case, variant, *, retrieval_only=False, context_mode=None, prepared_context=None):
        started = time.perf_counter()
        ticket = self.ticket(case)
        understanding = analyze_request(ticket)
        record = {"id": case["id"], "group": case["group"], "variant": variant,
                  "measurement_phase": "retrieval" if retrieval_only else "planning",
                  "expected": case["expected"], "context_mode": context_mode}
        decision = None
        try:
            docs, status = (copy.deepcopy(prepared_context) if prepared_context is not None
                            else self.retrieve(understanding.text, variant))
            if context_mode:
                docs = self.change_context(docs, context_mode)
            sources = [doc["source"] for doc in docs]
            relevant = case["relevant_sources"]
            ranks = [sources.index(source) + 1 for source in relevant if source in sources]
            record.update(retrieval=status, retrieved=[{key: doc.get(key) for key in
                ("id", "source", "offset", "resource", "codes", "version", "score", "retrieval_backend", "ranks", "snippet")} for doc in docs],
                retrieval_expected=bool(relevant), hit_at_1=bool(ranks and min(ranks) == 1),
                hit_at_3=bool(ranks and min(ranks) <= 3), hit_at_k=bool(ranks),
                reciprocal_rank=1 / min(ranks) if ranks else 0)
            if retrieval_only:
                return record
            # 只发送工单、相同的规则理解候选和各组上下文，case expected/relevant_sources 不进入模型。
            self.model.planning_use_sop = variant != "no_rag"
            decision = self.model.plan(ticket, understanding, docs)
            state = AgentState(ticket, understanding=understanding.to_dict(), retrieved_docs=docs)
            valid = True
            validation_error = None
            try:
                validate_choice(state, decision.choice, require_sop=variant != "no_rag", require_quotes=True)
            except ValueError as exc:
                valid = False
                validation_error = str(exc)
            choice = decision.choice
            correct = route_matches(choice, case["expected"])
            cited_sources = [doc["source"] for doc in docs if doc["id"] in choice.sop_refs]
            quoted_sources = [doc["source"] for doc in docs if doc["id"] in choice.sop_evidence
                              and choice.sop_evidence[doc["id"]].strip()
                              and choice.sop_evidence[doc["id"]] in doc["snippet"]]
            context_appropriate = True
            if context_mode in {"empty", "unrelated", "wrong_resource", "retired"}:
                context_appropriate = not choice.sop_refs and not choice.sop_evidence
            elif context_mode in {"updated", "conflicting"}:
                context_appropriate = set(choice.sop_refs) == {doc["id"] for doc in docs} and set(choice.sop_evidence) == set(choice.sop_refs)
            record.update(decision=decision.to_dict(), valid=valid, validation_error=validation_error,
                context_appropriate=context_appropriate,
                **score_decision(choice, case["expected"], valid=valid, context_appropriate=context_appropriate,
                                 threshold=self.cfg.decision_min_confidence),
                wrong_proposal=choice.next_step == "propose_action" and not correct,
                safe_abstention=choice.next_step == "manual_review" and case["expected"]["next_step"] == "propose_action",
                cited_sources=cited_sources, quoted_sources=quoted_sources,
                grounded_correct=correct and valid and bool(set(relevant).intersection(quoted_sources)),
                error=None)
        except Exception as exc:
            record.update(passed=False, error=type(exc).__name__, wrong_proposal=None,
                          decision=decision.to_dict() if decision else None)
        finally:
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return record

    def change_context(self, docs, mode):
        if mode == "empty":
            return []
        if mode == "unrelated":
            return [d for d in self.kb.lexical_search("密码重置", len(self.kb.documents)) if d["source"] == "password_reset.md"]
        if mode == "wrong_resource":
            return self.kb.lexical_search("ERP E417 身份冻结", 1)
        matched = [copy.deepcopy(doc) for doc in docs if doc.get("resource") == "vpn" and doc.get("codes") == "E417"]
        if len(matched) != 1:
            raise ValueError("stress fixture requires one matching active procedure")
        changed = copy.deepcopy(matched[0])
        changed["snippet"] = "# VPN E417 更新规程\n本 Demo 的 VPN E417 现在需要人工核查，处理结论：manual_review。停止自动处理，不登记普通故障、不申请权限。"
        changed["id"] = hashlib.sha256(changed["snippet"].encode()).hexdigest()
        changed["version"] = "demo-2026-10"
        changed["source"] = "controlled_update/vpn_e417.md"
        if mode == "updated":
            return [changed]
        if mode == "conflicting":
            changed["version"] = matched[0]["version"]
            return [matched[0], changed]
        if mode == "retired":
            matched[0]["status"] = "retired"
            return matched
        raise ValueError("unknown context mode")

    def e2e(self, case):
        started = time.perf_counter()
        audit = AuditStore(self.root / (case["id"] + ".db"))
        self.kb.hybrid = True
        service = TicketService(EnterpriseAgent(EnterpriseTools(self.kb, audit), audit, self.model,
                                               strict_model=True, settings=self.cfg))
        owner = Principal(user_id="alice", role="employee", department="IT")
        manager = Principal(user_id="manager", role="manager", department="IT")
        state = service.create(owner, title=case["title"], description=case["description"])
        expected = case["expected"]
        plan = state.ticket.execution_plan or {}
        correct_plan = all(plan.get(key) == expected[key] for key in ("action", "resource", "permission"))
        if state.ticket.status == "awaiting_clarification" and state.confirmation and correct_plan:
            state = service.clarify(owner, state.ticket.id, "确认执行", state.clarification_round)
        if state.ticket.status == "awaiting_approval" and correct_plan:
            state = service.approve(manager, state.approval.id, True)
        expected_status = "manual_review" if expected["next_step"] == "manual_review" else "closed"
        return {"id": case["id"], "variant": "hybrid", "group": case["group"], "measurement_phase": "e2e",
                "status": state.ticket.status, "expected_status": expected_status,
                "passed": state.ticket.status == expected_status and (correct_plan or expected_status == "manual_review"),
                "plan": state.ticket.execution_plan, "planning": state.planning,
                "understanding": state.understanding, "clarification": state.clarification,
                "error": state.error_info.to_dict() if state.error_info else None,
                "degradation": state.degradation, "trace": [row["node"] for row in state.trace],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def measured_count(records, field):
    """没有观测的指标不能用 0 代替；覆盖数量与观测值一起返回。"""
    values = [r[field] for r in records if r.get(field) is not None]
    return sum(bool(v) for v in values) if values else None


def planning_token_usage(records):
    """仅统计规划调用；部分缺失时不给出误导性的完整总数。"""
    values = [(r.get("decision") or {}).get("metadata", {}).get("usage", {}).get("total_tokens")
              for r in records]
    known = [v for v in values if type(v) is int and v >= 0]
    return {"tokens": sum(known) if known and len(known) == len(records) else None,
            "tokens_observed": sum(known) if known else None,
            "token_usage_n": len(known), "token_scope": "planning_calls_only"}


def summarize(records):
    summaries = []
    for variant in sorted({r["variant"] for r in records}):
        for group in ["all", "local_policy", "general", "unanswerable"]:
            subset = [r for r in records if r["variant"] == variant and (group == "all" or r["group"] == group)]
            if not subset:
                continue
            answerable = [r for r in subset if r.get("retrieval_expected")]
            raw = [{"match": r.get("raw_valid_correct", r.get("raw_route_correct", False) and r.get("valid", False))}
                   for r in subset if "raw_valid_correct" in r or "raw_route_correct" in r]
            fields = ("passed", "wrong_proposal", "safe_abstention", "grounded_correct")
            summaries.append(dict(variant=variant, group=group, n=len(subset),
                passed=measured_count(subset, "passed"), raw_route_matches=measured_count(raw, "match"),
                errors=sum(bool(r.get("error")) for r in subset),
                wrong_proposals=measured_count(subset, "wrong_proposal"),
                safe_abstentions=measured_count(subset, "safe_abstention"),
                grounded_correct=measured_count(subset, "grounded_correct"),
                metric_coverage={field: sum(r.get(field) is not None for r in subset) for field in fields},
                retrieval_n=len(answerable), hit_at_1=measured_count(answerable, "hit_at_1"),
                hit_at_3=measured_count(answerable, "hit_at_3"), hit_at_k=measured_count(answerable, "hit_at_k"),
                mrr=round(sum(r["reciprocal_rank"] for r in answerable) / len(answerable), 4) if answerable else None,
                **planning_token_usage(subset)))
    return summaries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["retrieval", "dev", "holdout", "stress", "e2e"], required=True)
    parser.add_argument("--variants", nargs="+", choices=["no_rag", "vector", "hybrid"], default=["no_rag", "vector", "hybrid"])
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--ids", nargs="+", help="仅定位指定案例；结果必须与完整实验分开报告")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    dataset = ROOT / "evals/rag_grounding.json"
    all_cases = json.loads(dataset.read_text(encoding="utf-8"))["cases"]
    split = "dev" if args.phase == "dev" else "holdout"
    cases = [case for case in all_cases if case["split"] == split]
    if args.ids:
        cases = [case for case in cases if case["id"] in args.ids]
    records = []
    report = {"phase": args.phase, "selected_ids": args.ids, "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "controller_policy": "confidence_threshold_applies_to_propose_action_only; bounded question guards tested in workflow tests",
        "code_sha256": {name: hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
                        for name in ["model_adapter.py", "knowledge.py", "planning.py", "rag_evaluation.py"]},
        "corpus_sha256": {str(p.relative_to(ROOT / "data/knowledge")): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / "data/knowledge").rglob("*.md"))},
        "scope": "合成内部规程；开发者编写的预留表达集，不是第三方盲测；规划节点受控实验与端到端结果分开；无真实工具副作用。",
        "controls": "相同模型、temperature=0、事实候选、资源与证据校验；仅改变上下文。无检索时安全弃答单列，不称为有害错误。标签不传给模型。",
        "cases": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("output already exists; use a new filename to preserve every run")
    def checkpoint():
        report["summary"] = summarize(records)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory() as temp:
        experiment = RagExperiment(temp, live=args.phase != "retrieval")
        report["model"] = experiment.cfg.deepseek_model
        report["embedding"] = "BAAI/bge-small-zh-v1.5 (local CPU weights)"
        report["parameters"] = {"temperature": 0, "timeout": 30, "retries": 0, "top_k": 3, "repeats": args.repeats}
        try:
            if args.phase == "stress":
                original = copy.deepcopy(next(c for c in all_cases if c["id"] == "dev_vpn_e417"))
                for mode in ("empty", "unrelated", "wrong_resource", "conflicting", "updated", "retired"):
                    case = copy.deepcopy(original)
                    case["id"] = "stress_" + mode
                    case["expected"] = dict(next_step="manual_review", action=None, resource=None, permission=None, missing_fact=None)
                    row = experiment.run(case, "hybrid", context_mode=mode)
                    records.append(row)
                    checkpoint()
                    print(case["id"], row.get("passed"), flush=True)
            elif args.phase == "e2e":
                selected = [c for c in all_cases if c["split"] == "dev"] + [c for c in cases if c["id"] in {"general_reports", "general_reset"}]
                if args.ids:
                    selected = [c for c in selected if c["id"] in args.ids]
                for case in selected:
                    records.append(experiment.e2e(case))
                    checkpoint()
                    print(case["id"], records[-1]["passed"], flush=True)
            else:
                for repeat in range(args.repeats):
                    for case in cases:
                        variants = list(args.variants)
                        random.Random(f"{repeat}:{case['id']}").shuffle(variants)
                        for variant in variants:
                            row = experiment.run(case, variant, retrieval_only=args.phase == "retrieval")
                            row["repeat"] = repeat + 1
                            records.append(row)
                            checkpoint()
                        print(f"{args.phase} repeat={repeat + 1} {case['id']}", flush=True)
        finally:
            experiment.close()
            checkpoint()
    print(json.dumps([r for r in summarize(records) if r["group"] == "all"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
