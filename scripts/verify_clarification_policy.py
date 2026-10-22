"""把保存的真实模型低置信度追问送入当前服务流程；无网络调用，不修改原始证据。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.audit import AuditStore
from app.auth import Principal
from app.clarification import check_offline
from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.planning import PlanningChoice, PlanningDecision
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent


class RecordedPlanner:
    supports_planning = True

    def __init__(self, raw):
        self.raw = raw

    def clarify(self, ticket, history=()):
        return check_offline(ticket, history)

    def plan(self, ticket, understanding, docs):
        return PlanningDecision(PlanningChoice.model_validate({k: self.raw[k] for k in PlanningChoice.model_fields if k in self.raw}),
                                source="recorded_deepseek", metadata={"replay": True, "network_calls": 0})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recorded_report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    original = json.loads(args.recorded_report.read_text(encoding="utf-8"))
    selected = [r for r in original["cases"] if r["id"] == "general_ambiguous" and r["variant"] == "no_rag"]
    case = next(c for c in json.loads((ROOT / "evals/rag_grounding.json").read_text(encoding="utf-8"))["cases"] if c["id"] == "general_ambiguous")
    results = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        cfg = get_settings(_env_file=None, llm_mode="offline", data_dir=root, planning_use_sop=False, rag_backend="lexical")
        for index, row in enumerate(selected):
            audit = AuditStore(root / f"{index}.db")
            agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(root / "kb", settings=cfg), audit), audit,
                                    RecordedPlanner(row["decision"]), settings=cfg)
            state = TicketService(agent).create(Principal(user_id="alice", role="employee", department="IT"),
                                               title=case["title"], description=case["description"])
            passed = (state.ticket.status == "awaiting_clarification" and state.clarification["missing_fields"] == ["previous_access"]
                      and state.ticket.execution_plan is None and state.action_result is None)
            results.append(dict(repeat=row["repeat"], confidence=row["decision"]["confidence"], status=state.ticket.status,
                                fields=state.clarification["missing_fields"], passed=passed, source=state.planning["source"]))
    report = {"scope": "保存的真实模型输出在当前工作流中回放；事实理解使用离线规则；没有再次调用模型，不是新盲测。",
              "source_file": args.recorded_report.name, "source_sha256": hashlib.sha256(args.recorded_report.read_bytes()).hexdigest(),
              "network_calls": 0, "total": len(results), "passed": sum(r["passed"] for r in results), "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
