"""VPN、ERP 和分支调查的真实 HTTP 完整链路；业务写入仍为模拟。"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from app.api import create_app
from app.audit import AuditStore
from app.auth import IdentityStore, initialize_demo_identities
from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.model_adapter import build_model_from_env
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from app.cli import build_action_adapter
from app.case_management import CaseLifecycle
from app.service import TicketService


def archive_synthetic_case(agent, identities, ticket_id, case_id):
    """End this isolated test case without declaring a real fault resolved.

    Different telemetry scenarios can intentionally use identical user text.
    Closing the previous synthetic case keeps business deduplication enabled and
    leaves the retrieval/model input unchanged for the next independent scenario.
    """
    raw = agent.audit.get_state(ticket_id)
    if raw["ticket"]["status"] not in CaseLifecycle.support_states:
        return
    service = TicketService(agent, identities)
    actor = identities.principal("admin")
    claimed = service.claim(actor, ticket_id, (raw["ticket"].get("assignment") or {}).get("revision", 0))
    service.resolve(actor, ticket_id, "archived", "合成验收场景已结束；归档以隔离下一场景，未确认真实故障解决。",
                    "synthetic:" + case_id, case_revision=claimed.ticket.case_revision)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--include-scope",action="store_true",help="Also verify the four deployment-specific runbooks end to end")
    parser.add_argument("--runbook-strategy",choices=["bm25","vector","hybrid"],help="Explicit retrieval mode for this temporary application")
    parser.add_argument("--case-id",action="append",help="Recheck named cases without replacing previous evidence")
    args=parser.parse_args()
    base=get_settings()
    cases=json.loads((ROOT/"evals/it_investigation.json").read_text(encoding="utf-8"))["cases"]
    cases += json.loads((ROOT/"evals/it_investigation_branches.json").read_text(encoding="utf-8"))["cases"]
    selected=[c for c in cases if c["id"] in {"t07","t04","b03"}]
    if args.include_scope:
        import copy
        for scoped in json.loads((ROOT/"evals/knowledge_value.json").read_text(encoding="utf-8"))["cases"]:
            if not scoped["id"].startswith("scope-"):
                continue
            case=copy.deepcopy(next(c for c in cases if c["id"]==("t04" if scoped["resource"]=="erp" else "t01")))
            case.update(id=scoped["id"],expected_runbook=scoped["expected_runbook"],handoff_topics=scoped["required_topics"])
            account=case["telemetry"]["resources"][case["resource"]]["accounts"]["alice"]
            texts={r["id"]:r["text"] for r in scoped["logs"]}
            for record in account["client_events"]+sum(account["sessions"].values(),[]):
                record["text"]=texts[record["id"]]
            selected.append(case)
    if args.case_id:
        unknown=set(args.case_id)-{case["id"] for case in selected}
        if unknown:
            parser.error("unknown selected case IDs: "+", ".join(sorted(unknown)))
        selected=[case for case in selected if case["id"] in args.case_id]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x",encoding="utf-8") as output,tempfile.TemporaryDirectory(prefix="it-http-") as folder:
        root=Path(folder); events=root/"events.json"
        cfg=get_settings(data_dir=root,db_path=root/"test.db",auth_file=root/"auth.json",kb_dir=base.kb_dir,
            chroma_dir=base.chroma_dir,investigation_file=events,runbooks_file=base.runbooks_file,
            runbooks_dir=base.runbooks_dir,
            runbook_strategy=args.runbook_strategy or base.runbook_strategy,
            diagnosis_mode="react",diagnosis_backend="investigation",llm_mode="deepseek")
        keys=json.loads(initialize_demo_identities(cfg.auth_file).read_text(encoding="utf-8"))
        audit=AuditStore(cfg.db_path)
        agent=EnterpriseAgent(EnterpriseTools(KnowledgeBase(cfg.kb_dir,settings=cfg),audit,build_action_adapter(cfg)),audit,
                              build_model_from_env(cfg),settings=cfg)
        rows=[]
        with TestClient(create_app(agent,identities=IdentityStore(cfg.auth_file))) as client:
            for case in selected:
                events.write_text(json.dumps(case["telemetry"]),encoding="utf-8")
                headers={"Authorization":"Bearer "+keys["alice"],"Idempotency-Key":"investigation-"+case["id"]}
                body={"title":case["resource"].upper()+" 故障","description":case["request"]+"页面报错超时。"}
                response=client.post("/tickets",headers=headers,json=body)
                accepted=response.json()
                ticket_id=accepted.get("ticket",{}).get("id","missing")
                staff_headers={"Authorization":"Bearer "+keys["manager"]}
                state=client.get(f"/tickets/{ticket_id}/state",headers=staff_headers).json()
                deadline=time.monotonic()+cfg.diagnosis_queue_timeout+cfg.diagnosis_worker_timeout+10
                while state.get("ticket",{}).get("investigation_status") in {"queued","running"} and time.monotonic()<deadline:
                    time.sleep(.05)
                    state=client.get(f"/tickets/{ticket_id}/state",headers=staff_headers).json()
                diagnostic=state.get("diagnosis") or {}
                replay=client.post("/tickets",headers=headers,json=body)
                denied=client.get(f"/tickets/{ticket_id}/state",headers={"Authorization":"Bearer "+keys["bob"]})
                checks={"created":response.status_code==201,"support_handoff":state.get("ticket",{}).get("status")=="awaiting_support",
                    "accepted_before_investigation":accepted.get("ticket",{}).get("status")=="awaiting_investigation" and accepted.get("diagnosis") is None,
                    "independent_attachment":(state.get("enrichment_result") or {}).get("status")=="succeeded",
                    "format":diagnostic.get("format")=="investigation_v1","source":diagnostic.get("source")=="deepseek",
                    "expected_runbook":diagnostic.get("finding")==case["expected_runbook"],
                    "citation_checked":diagnostic.get("evidence_verified") is True,
                    "not_claimed_semantically_verified":diagnostic.get("verified") is False,
                    "owner_and_resource":bool(diagnostic.get("observations")) and all(o["owner"]=="alice" and o["resource"]==case["resource"] for o in diagnostic.get("observations",[])),
                    "only_simulated_incident":state.get("ticket",{}).get("recommended_action")=="open_incident" and (state.get("action_result") or {}).get("simulated") is True,
                    "replay":replay.json()==accepted,"peer_denied":denied.status_code==404,
                    "requester_projection":"trace" not in accepted and "diagnosis" not in accepted}
                if case.get("handoff_topics"):
                    checks["required_handoff_topics"]=all(term in diagnostic.get("next_check","") for term in case["handoff_topics"])
                retrievals=[o.get("retrieval") or {} for o in diagnostic.get("observations",[]) if o.get("tool")=="search_runbooks"]
                checks["requested_retrieval_used"]=bool(retrievals) and all(
                    r.get("strategy")==cfg.runbook_strategy and not r.get("degraded") for r in retrievals)
                if cfg.action_backend == "http_incidents":
                    created = state.get("action_result") or {}
                    attached = state.get("enrichment_result") or {}
                    checks["outbound_http_receipts"] = bool(created.get("downstream_id")) and created.get("downstream_id") == attached.get("downstream_id")
                rows.append({"id":case["id"],"checks":checks,"passed":all(checks.values()),"state":state})
                print(json.dumps({"id":case["id"],"checks":checks},ensure_ascii=False),flush=True)
                archive_synthetic_case(agent, client.app.state.identities, ticket_id, case["id"])
        json.dump({"live":True,"model":cfg.deepseek_model,"runbook_strategy":cfg.runbook_strategy,"action_backend":cfg.action_backend,
            "scope":"HTTP auth, real LLM and RAG, synthetic raw logs, simulated writes, temporary DB",
            "passed":all(r["passed"] for r in rows),"records":rows},output,ensure_ascii=False,indent=2)
    if not all(r["passed"] for r in rows):
        raise SystemExit(1)


if __name__=="__main__":
    main()
