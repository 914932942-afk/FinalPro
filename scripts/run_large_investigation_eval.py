"""Run the large rules-vs-LLM investigation comparison without touching production data."""
import argparse
import hashlib
import json
import random
import sys
import tempfile
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.investigation_experiment import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=ROOT / "evals" / "it_investigation_large_20261011.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.fixture.read_text(encoding="utf-8"))["cases"]
    cfg = get_settings()
    schedule = [(case, variant) for case in cases for variant in ("rules", "fixed_llm")]
    random.Random(23).shuffle(schedule)
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(evaluate, case, variant, cfg) for case, variant in schedule]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"{index}/{len(futures)} {row['id']} {row['variant']} pass={row['contract_passed']} stop={row['session']['stop_reason']}", flush=True)
    summaries = []
    for variant in ("rules", "fixed_llm"):
        group = [row for row in rows if row["variant"] == variant]
        tokens = [row["tokens"] for row in group]
        summaries.append({
            "variant": variant,
            "n": len(group),
            "passed": sum(row["contract_passed"] for row in group),
            "pass_rate": round(sum(row["contract_passed"] for row in group) / max(len(group), 1), 4),
            "wrong_specific": sum(row["wrong_specific"] for row in group),
            "execution_failures": sum(row["session"]["stop_reason"] != "model_finish" for row in group),
            "llm_calls": sum(row["llm_calls"] for row in group),
            "tokens": sum(token for token in tokens if token is not None),
        })
    family_by_id = {case["id"]: case.get("family") for case in cases}
    family_breakdown = {}
    for family in sorted(set(family_by_id.values())):
        family_breakdown[family] = {}
        for variant in ("rules", "fixed_llm"):
            group = [row for row in rows if row["variant"] == variant and family_by_id[row["id"]] == family]
            family_breakdown[family][variant] = {"passed": sum(row["contract_passed"] for row in group), "n": len(group)}
    rules_by_id = {row["id"]: row for row in rows if row["variant"] == "rules"}
    llm_by_id = {row["id"]: row for row in rows if row["variant"] == "fixed_llm"}
    llm_wins = sum(llm_by_id[key]["contract_passed"] and not rules_by_id[key]["contract_passed"] for key in rules_by_id)
    rules_wins = sum(rules_by_id[key]["contract_passed"] and not llm_by_id[key]["contract_passed"] for key in rules_by_id)
    discordant = llm_wins + rules_wins
    sign_test_p = (sum(math.comb(discordant, k) for k in range(llm_wins, discordant + 1)) / (2 ** discordant)) if discordant else 1.0
    result = {
        "metadata": {
            "fixture": str(args.fixture),
            "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
            "model": cfg.deepseek_model,
            "metric": "expected runbook selection plus required current log citation; exact structured contract, not semantic quality of prose",
            "controls": "same 120 cases, same authenticated owner, same tools, same max budget; labels never sent to model",
            "limitations": [
                "synthetic authored telemetry, not enterprise production traffic",
                "15 variants per family; aggregate n is 120 runs but family-level clustering remains",
                "fixed_llm is the model comparison; ReAct is not included because previous data did not show accuracy superiority",
            ],
        },
        "summary": summaries,
        "family_breakdown": family_breakdown,
        "paired_comparison": {"llm_wins": llm_wins, "rules_wins": rules_wins, "ties": len(rules_by_id) - discordant, "two_sided_sign_test_upper_tail": sign_test_p},
        "records": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
