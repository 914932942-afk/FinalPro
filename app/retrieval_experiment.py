"""Reproducible local retrieval ablation; one fresh process per strategy.

No model API calls or business writes. Queries and labels are frozen before use.
Retrieval of an irrelevant document is NOT counted as a generated false answer.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time

from .config import get_settings
from .knowledge_experiment import observed_environments
from .runbook_knowledge import RunbookKnowledge

ROOT = Path(__file__).resolve().parents[1]


def memory():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        values = Counters(); values.cb = ctypes.sizeof(values)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(values), values.cb):
            return {"rss_mib": round(values.WorkingSetSize/1048576, 2),
                    "peak_rss_mib": round(values.PeakWorkingSetSize/1048576, 2)}
    return {"rss_mib": None, "peak_rss_mib": None}


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


def score(case, docs, universe):
    expected = case["expected_runbook"]
    ids = [d["id"] for d in docs]
    eligible = {d["id"] for d in RunbookKnowledge.scope(universe, observed_environments(case))}
    rank = ids.index(expected)+1 if expected in ids else None
    return {"recalled": expected in ids if expected else None,
            "reciprocal_rank": 1/rank if rank else 0 if expected else None,
            "scope_leaks": [ident for ident in ids if ident not in eligible],
            "unanswerable_candidates": len(ids) if not expected else None}


def run_variant(fixture, variant):
    cfg = get_settings(_env_file=None, runbook_strategy="bm25")
    kb = RunbookKnowledge(cfg.runbooks_dir, settings=cfg)
    strategy = variant
    if variant.startswith("legacy_"):
        from .retrieval_tuning import LegacyRunbookKnowledge
        kb = LegacyRunbookKnowledge(cfg.runbooks_dir, settings=cfg)
        strategy = variant.removeprefix("legacy_")
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    random.Random(73).shuffle(cases)
    snapshots = {resource: kb.capture(resource) for resource in {c["resource"] for c in cases}}
    before = memory()
    def search(case, k):
        records = snapshots[case["resource"]]
        if variant == "full_context":
            return kb.scope(records, observed_environments(case)), {"strategy": variant}
        return kb.search(case["query"], records, strategy=strategy, limit=k,
                         environments=observed_environments(case))
    start = time.perf_counter()
    _, first_status = search(cases[0], 2)
    cold_ms = (time.perf_counter()-start)*1000
    # Warm all resource/environment document subsets before measuring query encoding.
    for case in cases:
        search(case, 2)
    rows = []
    for case in cases:
        timings = []
        for _ in range(3):
            with kb.kb._lock:
                kb._queries.clear()  # explicitly measure warm model but uncached query
            start = time.perf_counter(); docs, status = search(case, 2)
            timings.append((time.perf_counter()-start)*1000)
        start = time.perf_counter(); search(case, 2)
        cached_ms = (time.perf_counter()-start)*1000
        for k in ([None] if variant == "full_context" else [1,2,3]):
            docs, status = search(case, k or 2)
            rows.append({"id": case["id"], "group": case["group"], "variant": variant, "top_k": k,
                "retrieved_ids": [d["id"] for d in docs], "retrieval": status,
                "context_characters": sum(len(d["text"]) for d in docs),
                "warm_uncached_ms": timings if k in (None, 2) else None,
                "warm_cached_ms": cached_ms if k in (None, 2) else None,
                **score(case, docs, snapshots[case["resource"]])})
    return {"variant":variant,"cold_first_retrieval_ms":cold_ms,"cold_status":first_status,
            "memory_before":before,"memory_after":memory(),"records":rows}


def summarize(rows):
    summaries = []
    for variant, k in dict.fromkeys((r["variant"], r["top_k"]) for r in rows):
        group = [r for r in rows if r["variant"] == variant and r["top_k"] == k]
        positives = [r for r in group if r["recalled"] is not None]
        timings = [t for r in group for t in (r["warm_uncached_ms"] or [])]
        summaries.append({"variant":variant,"top_k":k,"answerable_n":len(positives),
            "recalled":sum(r["recalled"] for r in positives),
            "mrr":sum(r["reciprocal_rank"] for r in positives)/len(positives) if positives else None,
            "scope_leaks":sum(len(r["scope_leaks"]) for r in group),
            "degraded":sum(bool(r["retrieval"].get("degraded")) for r in group),
            "p50_warm_uncached_ms":percentile(timings,.5),"p95_warm_uncached_ms":percentile(timings,.95),
            "mean_context_characters":sum(r["context_characters"] for r in group)/len(group),
            "by_group":{g:{"n":len([r for r in positives if r["group"]==g]),
                "recalled":sum(r["recalled"] for r in positives if r["group"]==g)} for g in sorted({r["group"] for r in group})}})
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,default=ROOT/"evals/retrieval_probe.json")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--variants",nargs="+",choices=["legacy_bm25","legacy_hybrid","bm25","vector","hybrid","full_context"],
                        default=["bm25","vector","hybrid","full_context"])
    parser.add_argument("--worker",choices=["legacy_bm25","legacy_hybrid","bm25","vector","hybrid","full_context"])
    args = parser.parse_args()
    args.fixture = args.fixture.resolve()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists():
        parser.error("output already exists; preserve earlier experiments")
    if args.worker:
        result = run_variant(args.fixture,args.worker)
    else:
        files = [ROOT/"app/runbook_knowledge.py", Path(__file__), args.fixture]
        files += list((ROOT/"data/knowledge/runbooks").glob("*.md"))
        hashes = lambda: {str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p):
                           hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        initial = hashes()
        results = []
        for variant in args.variants:
            destination = args.output.with_name(args.output.stem+"-"+variant+".json")
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            subprocess.run([sys.executable,"-m","app.retrieval_experiment","--worker",variant,
                            "--fixture",str(args.fixture),"--output",str(destination)],check=True,cwd=ROOT,env=env)
            results.append(json.loads(destination.read_text(encoding="utf-8")))
            print(variant,"complete",flush=True)
        rows = [row for result in results for row in result["records"]]
        result = {"scope":"Synthetic frozen retrieval probe, not a blind enterprise test. Repeated timings are not independent quality cases. Candidate presence on unanswerable queries is not a false generated answer.",
            "hashes":initial,"unchanged":initial==hashes(),"summary":summarize(rows),"workers":results}
    with args.output.open("x",encoding="utf-8") as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)


if __name__ == "__main__":
    main()
