"""VPN 诊断消融：原流程 / 固定查询 / 模型 ReAct / 自适应规则。仅合成只读数据。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import tempfile
import time

from .config import get_settings
from .diagnostics import DiagnosticRunner, DeepSeekDiagnosticModel, LocalVPNDiagnostics, RuleDiagnosticModel

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("original", "fixed", "react", "adaptive_rule")


def evaluate_case(case, variant, repeat, settings, *, live=False):
    start = time.perf_counter()
    if variant == "original":
        # 现有登记流程不做遥测归因；不为它凭空增加一次模型调用。
        session = {"finding": "inconclusive", "verified": False, "source": "no_diagnostics",
                   "tool_calls": 0, "model_calls": 0, "observations": [], "decisions": [],
                   "stop_reason": "original_has_no_telemetry_diagnosis"}
    else:
        with tempfile.TemporaryDirectory(prefix="vpn-eval-") as folder:
            path = Path(folder) / "telemetry.json"
            # 标签、case ID、期望工具数不进入工具输入或模型上下文。
            path.write_text(json.dumps(case["telemetry"]), encoding="utf-8")
            model = DeepSeekDiagnosticModel(settings) if live and variant != "adaptive_rule" else RuleDiagnosticModel()
            session = DiagnosticRunner(LocalVPNDiagnostics(path), model,
                mode="fixed" if variant == "fixed" else "rule" if model.source == "rule" else "react",
                deadline_seconds=settings.diagnosis_deadline).run(
                    text=case["request"], owner="alice", binding="synthetic-experiment")
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    llm_calls = session["model_calls"] if session["source"] == "deepseek" else 0
    usages = [d["metadata"].get("usage", {}).get("total_tokens") for d in session["decisions"]]
    # 丢失 usage 或失败的调用不当作 0 token。
    tokens = (sum(usages) if len(usages) == llm_calls and all(type(n) is int for n in usages)
              else None) if llm_calls else 0
    expected = case["expected_finding"]
    finding = session["finding"]
    return {"id": case["id"], "repeat": repeat, "variant": variant, "expected_finding": expected,
            "finding": finding, "passed": finding == expected,
            "unsupported_specific_finding": finding != "inconclusive" and finding != expected,
            "specific_case": expected != "inconclusive", "elapsed_ms": elapsed, "llm_calls": llm_calls,
            "total_tokens": tokens, "tool_calls": session["tool_calls"], "session": session}


def summarize(rows):
    summaries = []
    for variant in VARIANTS:
        group = [r for r in rows if r["variant"] == variant]
        if not group:
            continue
        specific = [r for r in group if r["specific_case"]]
        tokens = [r["total_tokens"] for r in group]
        summaries.append({"variant": variant, "n": len(group), "passed": sum(r["passed"] for r in group),
            "specific_case_n": len(specific), "correct_specific_n": sum(r["passed"] for r in specific),
            "unsupported_specific_n": sum(r["unsupported_specific_finding"] for r in group),
            "tool_calls": sum(r["tool_calls"] for r in group), "llm_calls": sum(r["llm_calls"] for r in group),
            "total_tokens": sum(tokens) if all(t is not None for t in tokens) else None,
            "usage_missing_rows": sum(t is None for t in tokens),
            "mean_latency_ms": round(sum(r["elapsed_ms"] for r in group)/len(group), 2),
            "non_finish_n": sum(r["session"]["stop_reason"] != "model_finish" for r in group)
                if variant != "original" else 0})
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="fixed/react 使用真实 DeepSeek，有 API 用量")
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = get_settings()
    if args.live and not cfg.deepseek_api_key.get_secret_value().strip():
        parser.error("live requires configured DEEPSEEK_API_KEY")
    fixture = ROOT / "evals/vpn_diagnostics.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    schedule = [(c, v, r) for r in range(1, args.repeats+1) for c in cases for v in VARIANTS]
    random.Random(20260929).shuffle(schedule)
    metadata = {"live": args.live, "model": cfg.deepseek_model if args.live else None,
        "temperature": 0, "case_n": len(cases), "repeats": args.repeats, "workers": args.workers,
        "deadline_seconds": cfg.diagnosis_deadline, "max_tools": 3, "max_model_steps": 4,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "app").glob("*.py"))},
        "scope": "synthetic read-only VPN telemetry; diagnostic subflow only, excludes upstream planning/RAG and incident writing",
        "limitations": ["authored synthetic cases, not held-out enterprise data", "repeats are not independent cases",
                        "same field contracts used in verifier and rule baseline", "local tool latency is not enterprise API latency",
                        "offline fixed/react use rule stub, not evidence of LLM capability",
                        "success measures supported evidence attachment, not repair or exclusive root-cause accuracy"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal_path = args.output.with_suffix(".jsonl")
    # 独占输出与逐条落盘；中断/失败结果不能被重跑悄悄覆盖。
    with args.output.open("x", encoding="utf-8") as report, journal_path.open("x", encoding="utf-8") as journal:
        journal.write(json.dumps({"metadata": metadata}, ensure_ascii=False)+"\n")
        journal.flush()
        rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs = [pool.submit(evaluate_case, c, v, r, cfg, live=args.live) for c, v, r in schedule]
            for job in as_completed(jobs):
                row = job.result()
                rows.append(row)
                journal.write(json.dumps(row, ensure_ascii=False)+"\n")
                journal.flush()
                print(f"{len(rows)}/{len(schedule)} {row['variant']} {row['id']} r{row['repeat']} passed={row['passed']} tools={row['tool_calls']}", flush=True)
        result = {"metadata": metadata, "summary": summarize(rows),
                  "records": sorted(rows, key=lambda r: (r["id"], r["repeat"], r["variant"]))}
        json.dump(result, report, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
