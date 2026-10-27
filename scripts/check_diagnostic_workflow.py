"""通过 HTTP 认证入口检查完整 ReAct 工单链路，临时数据库和身份，不改演示工单。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.api import create_app
from app.audit import AuditStore
from app.auth import IdentityStore, initialize_demo_identities
from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.model_adapter import build_model_from_env
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = get_settings()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output, tempfile.TemporaryDirectory(prefix="diagnostic-http-") as tmp:
        root = Path(tmp)
        telemetry = root / "vpn.json"
        cfg = get_settings(data_dir=root, db_path=root/"test.db", auth_file=root/"auth.json",
            kb_dir=base.kb_dir, chroma_dir=base.chroma_dir, diagnosis_file=telemetry, diagnosis_mode="react", diagnosis_backend="structured",
            llm_mode="deepseek" if args.live else "offline", rag_backend=base.rag_backend if args.live else "lexical")
        key_path = initialize_demo_identities(cfg.auth_file)
        keys = json.loads(key_path.read_text(encoding="utf-8"))
        audit = AuditStore(cfg.db_path)
        agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(cfg.kb_dir, settings=cfg), audit), audit,
                                model=build_model_from_env(cfg), settings=cfg)
        rows = []
        with TestClient(create_app(agent, identities=IdentityStore(cfg.auth_file))) as client:
            for service, error, expected in [("outage", "service_unavailable", "service_outage"),
                                              ("healthy", "tls_certificate_expired", "certificate_expired")]:
                telemetry.write_text(json.dumps({"simulated": True, "service": service,
                    "accounts": {"alice": {"enabled": True, "vpn_access": True, "recent_error": error}}}), encoding="utf-8")
                headers = {"Authorization": "Bearer " + keys["alice"], "Idempotency-Key": expected}
                body = {"title": "VPN 故障", "description": f"VPN 连接失败，请登记排查。本次报错：{error}。"}
                response = client.post("/tickets", headers=headers, json=body)
                accepted = response.json()
                ticket_id = accepted.get("ticket", {}).get("id", "missing")
                staff_headers = {"Authorization": "Bearer " + keys["manager"]}
                state = client.get(f"/tickets/{ticket_id}/state", headers=staff_headers).json()
                deadline = time.monotonic() + cfg.diagnosis_queue_timeout + cfg.diagnosis_worker_timeout + 10
                while state.get("ticket", {}).get("investigation_status") in {"queued", "running"} and time.monotonic() < deadline:
                    time.sleep(.05)
                    state = client.get(f"/tickets/{ticket_id}/state", headers=staff_headers).json()
                replay = client.post("/tickets", headers=headers, json=body)
                peer = client.get(f"/tickets/{ticket_id}/state", headers={"Authorization": "Bearer " + keys["bob"]})
                diagnosis = state.get("diagnosis") or {}
                checks = {"created": response.status_code == 201, "support_handoff": state.get("ticket", {}).get("status") == "awaiting_support",
                    "correct_finding": diagnosis.get("finding") == expected,
                    "diagnostic_source": diagnosis.get("source") == ("deepseek" if args.live else "rule"),
                    "registered_only": state.get("ticket", {}).get("recommended_action") == "open_incident",
                    "write_simulated": (state.get("action_result") or {}).get("simulated") is True,
                    "replay_equal": replay.json() == accepted, "peer_denied": peer.status_code == 404,
                    "requester_projection": "trace" not in accepted and "diagnosis" not in accepted}
                rows.append({"expected": expected, "checks": checks, "passed": all(checks.values()), "state": state})
                print(json.dumps({"expected": expected, "checks": checks}, ensure_ascii=False), flush=True)
        report = {"live": args.live, "model": cfg.deepseek_model if args.live else None,
            "rag_backend": cfg.rag_backend, "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT/"app").glob("*.py"))},
            "scope": "full HTTP workflow with synthetic telemetry and simulated incident writes; fresh temporary identity/database",
            "passed": all(r["passed"] for r in rows), "records": rows}
        json.dump(report, output, ensure_ascii=False, indent=2)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
