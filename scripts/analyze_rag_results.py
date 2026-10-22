"""从已保存模型响应重算原始规划/实际控制器路由；不重试请求、不覆盖原始报告。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.planning import PlanningChoice
from app.rag_evaluation import score_decision, summarize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    report = json.loads(args.input.read_text(encoding="utf-8"))
    report["derived_from"] = {"file": args.input.name, "sha256": hashlib.sha256(args.input.read_bytes()).hexdigest()}
    report["scoring_note"] = "不重跑模型。使用保存的原始响应和当时的 valid 标记，只重算当前置信度策略（仅拦截 propose_action）；不代表重新验证当前引用校验、澄清轮次或端到端行为。original_passed 保留原判断，历史报告不修改。"
    report["controller_policy"] = "confidence_threshold_applies_to_propose_action_only"
    for row in report["cases"]:
        raw = row.get("decision")
        row["original_passed"] = row.get("passed")
        if not raw or row.get("error"):
            continue
        choice = PlanningChoice.model_validate({k: raw[k] for k in PlanningChoice.model_fields if k in raw})
        row.update(score_decision(choice, row["expected"], valid=row["valid"], context_appropriate=row.get("context_appropriate", True)))
    report["summary"] = summarize(report["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([row for row in report["summary"] if row["group"] == "all"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
