"""固定排序前缀的 top_k 消融。默认仅检索；--live 才调用真实模型，无业务工具副作用。"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import tempfile
import time

from .policy import analyze_request
from .rag_evaluation import ROOT, RagExperiment, summarize


def context_digest(docs):
    payload = [{"id": d["id"], "snippet": d["snippet"]} for d in docs]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def percentile(values, fraction):
    """Nearest-rank percentile；小样本的 P95 接近最大值，不作为稳定 SLA。"""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


class TopKExperiment(RagExperiment):
    def prepare(self, case, max_k):
        text = analyze_request(self.ticket(case)).text
        self.kb.hybrid = True
        start = time.perf_counter()
        docs, status = self.kb.search_with_status(text, max_k)
        if status.get("degraded") or status.get("backend") != "chroma":
            raise RuntimeError("top_k experiment requires real vector retrieval without fallback")
        return docs, {**status, "shared_retrieval_ms": round((time.perf_counter() - start) * 1000, 2),
                      "candidate_window": max(12, max_k * 4), "ranking_digest": context_digest(docs)}

    def evaluate_prefix(self, case, docs, status, k, repeat, *, live):
        selected = docs[:k]
        row = self.run(case, "hybrid", retrieval_only=not live, prepared_context=(selected, status))
        row.update(variant=f"top_k={k}", top_k=k, repeat=repeat, returned_chunks=len(selected),
                   context_chars=sum(len(d["snippet"]) for d in selected),
                   context_sha256=context_digest(selected))
        decision = row.get("decision")
        if decision is not None:
            refs, quotes = decision.get("sop_refs", []), decision.get("sop_evidence", {})
            available = {doc["id"]: doc for doc in selected}
            row["citation_count"] = len(refs)
            # 这是引用完整性，不宣称任意自然语言语义蕴含已得到证明。
            row["citation_integrity"] = (all(ref in available and bool(quotes.get(ref, "").strip())
                and quotes[ref] in available[ref]["snippet"] for ref in refs)
                and set(quotes) == set(refs)) if refs or quotes else None
        return row


def summarize_topk(records):
    result = summarize(records)
    for summary in result:
        rows = [r for r in records if r["variant"] == summary["variant"]
                and (summary["group"] == "all" or r["group"] == summary["group"])]
        elapsed = [r["elapsed_ms"] for r in rows if r.get("elapsed_ms") is not None]
        measured = [r for r in rows if r.get("citation_integrity") is not None]
        summary.update(
            planning_success_rate=(summary["passed"] / len(rows)
                                   if any(r.get("measurement_phase") == "planning" for r in rows) else None),
            mean_returned_chunks=sum(r["returned_chunks"] for r in rows) / len(rows),
            mean_context_chars=sum(r["context_chars"] for r in rows) / len(rows),
            citation_integrity_n=len(measured),
            citation_integrity_rate=sum(r["citation_integrity"] for r in measured) / len(measured) if measured else None,
            evaluation_p50_ms=percentile(elapsed, .5), evaluation_p95_ms=percentile(elapsed, .95),
            latency_scope="planning_and_validation_only_excludes_shared_retrieval_and_indexing")
    return result


def paired_comparisons(records, ks, baseline=3):
    indexed = {(r["id"], r["repeat"], r["top_k"]): r for r in records}
    baselines = [r for r in records if r["top_k"] == baseline]
    result = []
    for k in ks:
        if k == baseline:
            continue
        pairs = [(b, indexed[(b["id"], b["repeat"], k)]) for b in baselines
                 if (b["id"], b["repeat"], k) in indexed]
        measured = [(b, r) for b, r in pairs if b.get("measurement_phase") == "planning"
                    and r.get("measurement_phase") == "planning"]
        result.append({"baseline_k": baseline, "candidate_k": k, "paired_n": len(pairs),
            "different_context_n": sum(b["context_sha256"] != r["context_sha256"] for b, r in pairs),
            "planning_measured_n": len(measured),
            "improved": [f"{r['id']}#r{r['repeat']}" for b, r in measured if not b.get("passed") and r.get("passed")],
            "regressed": [f"{r['id']}#r{r['repeat']}" for b, r in measured if b.get("passed") and not r.get("passed")],
            "error_pair_n": sum(bool(b.get("error") or r.get("error")) for b, r in pairs)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="真实 DeepSeek 规划调用，有 API 用量")
    parser.add_argument("--ks", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--ids", nargs="+", help="定位子集；报告明确标注，不能冒充全量")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(set(args.ks)) != len(args.ks) or 3 not in args.ks or not all(1 <= k <= 10 for k in args.ks):
        parser.error("ks must be unique integers from 1 to 10 and include baseline 3")
    os.environ["HF_HUB_OFFLINE"] = "1"
    dataset = ROOT / "evals/rag_grounding.json"
    cases = [c for c in json.loads(dataset.read_text(encoding="utf-8"))["cases"] if c["split"] == "holdout"]
    if args.ids:
        unknown = set(args.ids) - {c["id"] for c in cases}
        if unknown:
            parser.error("unknown holdout case IDs: " + ", ".join(sorted(unknown)))
        cases = [c for c in cases if c["id"] in args.ids]
    records = []
    report = {"experiment": "fixed_ranking_context_top_k", "status": "running", "live": args.live,
        "scope": "既有合成集的探索性消融，非新盲测；仅规划节点，无工具调用、审批或多轮任务成功率。",
        "controls": "每例真实混合检索一次，固定排序后截取前 k 条；不改变知识、事实候选、提示词、模型参数和执行约束。标签不送模型。",
        "interpretation": "不同 k 的上下文可能相同。没有提升或出现退化均保留；不自动修改生产默认值。耗时不含共享检索与索引。",
        "parameters": {"ks": args.ks, "baseline_k": 3, "repeats": args.repeats, "temperature": 0,
                       "candidate_window": max(12, max(args.ks) * 4), "seed": "topk-v1",
                       "case_count": len(cases), "selected_ids": args.ids,
                       "planned_model_calls": len(cases) * len(args.ks) * args.repeats if args.live else 0},
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "app").glob("*.py"))},
        "corpus_sha256": {p.relative_to(ROOT / "data/knowledge").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / "data/knowledge").rglob("*.md"))}, "cases": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 排他创建，绝不覆盖先前运行的证据。
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    except FileExistsError:
        parser.error("output already exists; choose a new output filename")

    def checkpoint():
        report["summary"] = summarize_topk(records)
        report["paired"] = paired_comparisons(records, args.ks)
        temp_output = args.output.with_suffix(args.output.suffix + ".tmp")
        temp_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_output.replace(args.output)

    try:
        with tempfile.TemporaryDirectory() as temp:
            experiment = TopKExperiment(temp, live=args.live)
            try:
                report["model"] = experiment.cfg.deepseek_model if args.live else None
                report["embedding"] = "BAAI/bge-small-zh-v1.5 local CPU"
                report["parameters"].update(confidence_threshold=experiment.cfg.decision_min_confidence,
                    chunk_size=experiment.kb.chunk_size, overlap=experiment.kb.overlap, retries=0,
                    timeout=experiment.cfg.deepseek_timeout)
                # 保存逐例上下文后再开始规划，确保同一输入各组来自同一排序。
                prepared = {case["id"]: experiment.prepare(case, max(args.ks)) for case in cases}
                report["prepared_contexts"] = {ident: {"docs": docs, "status": status}
                                                for ident, (docs, status) in prepared.items()}
                checkpoint()
                for repeat in range(1, args.repeats + 1):
                    ordered = list(cases)
                    random.Random(f"topk-v1:{repeat}").shuffle(ordered)
                    for case in ordered:
                        ks = list(args.ks)
                        random.Random(f"topk-v1:{repeat}:{case['id']}").shuffle(ks)
                        docs, status = prepared[case["id"]]
                        for k in ks:
                            row = experiment.evaluate_prefix(case, docs, status, k, repeat, live=args.live)
                            records.append(row)
                            checkpoint()
                            print(f"{len(records)} {case['id']} k={k} chunks={row['returned_chunks']} passed={row.get('passed')}", flush=True)
                report["status"] = "completed_with_errors" if any(r.get("error") for r in records) else "completed"
            finally:
                experiment.close()
    except BaseException as exc:
        report["status"], report["run_error"] = "interrupted", type(exc).__name__
        raise
    finally:
        checkpoint()
    print(json.dumps(report["paired"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
