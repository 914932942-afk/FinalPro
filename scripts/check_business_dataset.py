"""Check a frozen dataset; model calls require explicit --live and a case limit."""
import argparse
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.scenario_dataset import ScenarioDatasetEvaluator, ScenarioDatasetValidator, digest, read_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/business_scenarios_v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Call the configured model; consumes API usage")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=["development", "regression", "challenge", "all"], default="all")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new report filename; previous evidence is preserved")
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    if args.live and args.limit is None:
        parser.error("--live requires an explicit --limit to bound API usage")
    validation = ScenarioDatasetValidator().validate(args.dataset)
    inputs = read_jsonl(args.dataset / "inputs.jsonl")
    labels = {r["id"]: r for r in read_jsonl(args.dataset / "labels.jsonl")}
    selected = [r for r in inputs if r["suite"] == "diagnosis"
                and (args.split == "all" or labels[r["id"]]["split"] == args.split)
                and (not args.live or labels[r["id"]]["domain"] != "data_availability")]
    if args.limit:
        # Round-robin scenario families so small pilots do not select ten variants
        # of the very first mechanism. Same inputs/labels are never fed together.
        families = {}
        for row in selected:
            families.setdefault(labels[row["id"]]["scenario"], []).append(row)
        selected = [rows[i] for i in range(max(map(len, families.values()), default=0))
                    for rows in families.values() if i < len(rows)][:args.limit]
    cfg = get_settings()
    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    current_knowledge = {p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() for p in sorted(cfg.runbooks_dir.glob("*.md"))}
    if current_knowledge != manifest["knowledge_sha256"]:
        parser.error("knowledge differs from the frozen release; create a new dataset/evaluation version")
    if args.live and not cfg.deepseek_api_key.get_secret_value().strip():
        parser.error("model credentials are not configured")
    evaluator = ScenarioDatasetEvaluator(cfg)
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive journal preserves results even if a later model or process fails.
    journal = args.output.with_suffix(args.output.suffix + ".jsonl")
    with journal.open("x", encoding="utf-8") as stream:
        for case in selected:
            row = evaluator.live(case, labels[case["id"]]) if args.live else evaluator.check_adapter(case, labels[case["id"]])
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
    def metric_summary(records):
        if not records:
            return {"n": 0}
        sessions = [r.get("session", {}) for r in records if r.get("session")]
        assessments = {key: sum(s.get("assessment") == key for s in sessions) for key in ("supported_hypothesis", "insufficient")}
        return {
            "n": len(records),
            "contract_passed": sum(bool(r.get("contract_passed", r.get("passed", False))) for r in records),
            "pass_rate": round(sum(bool(r.get("contract_passed", r.get("passed", False))) for r in records) / len(records), 4),
            "assessment_counts": assessments,
            "awaiting_user_info": sum(s.get("status") == "awaiting_user_info" for s in sessions),
            "data_gaps": sum(str(s.get("stop_reason", "")).startswith("data_gap:") for s in sessions),
            "invalid_citations": sum(s.get("stop_reason") == "invalid_citation" for s in sessions),
            "tool_budget_stops": sum(s.get("stop_reason") == "tool_budget" for s in sessions),
            "repeated_tool_stops": sum(s.get("stop_reason") == "repeated_tool" for s in sessions),
            "time_scopes_bounded": sum((s.get("time_scope") or {}).get("status") == "bounded" for s in sessions),
            "semantic_quality_measured": False,
        }
    by_scenario = {}
    for row in rows:
        scenario = labels[row["id"]]["scenario"]
        by_scenario.setdefault(scenario, []).append(row)
    breakdown = {scenario: metric_summary(group) for scenario, group in sorted(by_scenario.items())}
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": "live_diagnostic_subflow" if args.live else "offline_adapter_contracts",
              "integrity": validation, "selected_ids": [r["id"] for r in selected], "input_sha256": digest(inputs),
              "split": args.split, "executed": len(rows), "passed": sum(r.get("contract_passed", r.get("passed", False)) for r in rows),
              "api_calls_enabled": args.live, "model": cfg.deepseek_model if args.live else None,
              "semantic_quality_measured": False, "business_workflows_executed": False,
              "summary": metric_summary(rows), "scenario_breakdown": breakdown,
              "workflow_specifications_not_executed": sum(r["suite"] == "workflow" for r in inputs),
              "limitations": ["No model accuracy claim from offline success", "No independent human judgement",
                              "No live IAM or incident writes", "Variant-level counts are correlated"], "records": rows}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("mode", "executed", "passed", "semantic_quality_measured", "business_workflows_executed")}, ensure_ascii=False))
    if report["passed"] != report["executed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
