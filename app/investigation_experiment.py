"""原始 IT 日志调查消融。测试标签不进入模型或工具；规则、固定 LLM、ReAct 同工具同预算。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import re
import tempfile
import time

from .config import get_settings
from .investigation import InvestigationTools, InvestigationRunner, InvestigationModel, InvestigationStep, Citation, validate_citations

ROOT = Path(__file__).resolve().parents[1]


class FrozenRules:
    """常见日志的正则基线，显式处理时间选择和常见否定；仅维护在开发集上。"""
    source = "rule"
    patterns = {
        "kb-v01": r"NXDOMAIN|name (?:lookup|resolution) failed|域名解析失败|找不到.{0,6}名称",
        "kb-v02": r"route-nopull.*(?:skipped|not installed)|subnet overlap|网段冲突|路由覆盖|走本地网卡",
        "kb-v03": r"certificate (?:has )?expired|证书(?:已)?过期|VERIFY ERROR.*expired",
        "kb-e01": r"connection pool timeout|(?:pool|connection).*(?:wait|timed out)|连接池.*超时",
        "kb-e02": r"invalid audience|audience mismatch|audience=[\w-]+, expected [\w-]+|aud=.*expected|令牌受众.*不匹配",
        "kb-e03": r"deadlock detected|circular wait|死锁|循环等待",
    }

    def finish(self, observations, resource):
        logs = [r for o in observations if o["tool"] == "session_events" for r in o["records"]]
        docs = {r["id"]: r for o in observations for r in o["records"] if r["kind"] == "runbook"}
        matches = []
        for key, pattern in self.patterns.items():
            for record in logs:
                sentences = re.split(r"[;；。\n]", record["text"])
                positive = [s for s in sentences if not re.search(r"\bno\b|\bnot\b|已恢复|已通过|未超时", s, re.I)]
                if key in docs and re.search(pattern, " ".join(positive), re.I):
                    matches.append((key, record))
        keys = {key for key, _ in matches}
        if len(keys) == 1:
            key, record = matches[0]
            return InvestigationStep(kind="finish", tool=None, argument="", assessment="supported_hypothesis", runbook_id=key,
                summary="日志匹配已维护的常见故障模式，建议人工核查对应规程。", next_check=docs[key]["text"][:350],
                citations=[Citation(record_id=record["id"], quote=record["text"][:500]),
                           Citation(record_id=key, quote=docs[key]["text"][:500])], reason="规则匹配唯一已知模式")
        return InvestigationStep(kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
            summary="当前规则不能唯一确定适用规程。", next_check="人工关联原始日志与排障文档。", citations=[], reason="无唯一规则匹配")


def collect_fixed(adapter, text):
    """相同工具预算：先取客户端，按用户明示时间匹配会话，再基于日志全文截取查询规程。"""
    observations = []
    observations.append(adapter.read("client_events", "", "alice", observations, timeout=60))
    records = observations[0]["records"]
    times = re.findall(r"\b\d\d:\d\d\b", text)
    relevant = [r for r in records if any(t in r["text"] for t in times)] if times else records
    handles = [ref for ref in observations[0]["session_refs"] if any(ref in r["text"] for r in relevant)]
    queue = list((handles or observations[0]["session_refs"])[-3:])
    seen = set()
    while queue and len(observations) < 4:
        ref = queue.pop(0)
        if ref in seen:
            continue
        seen.add(ref)
        obs = adapter.read("session_events", ref, "alice", observations, timeout=60)
        observations.append(obs)
        # 固定基线也能沿已知引用跟进；常见失败词优先，随后按返回顺序，避免故意削弱基线。
        failed = [r["text"] for r in obs["records"] if re.search(r"error|fail|timeout|错误|失败|超时|拒绝", r["text"], re.I)]
        children = sorted(obs["session_refs"], key=lambda key: not any(key in text for text in failed))
        queue.extend(key for key in children if key not in seen)
    current = " ".join(r["text"] for o in observations[1:] for r in o["records"])
    query = (current or text)[-200:]
    observations.append(adapter.read("search_runbooks", query, "alice", observations, timeout=60))
    return observations


def evaluate(case, variant, cfg):
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="it-investigation-") as folder:
        path = Path(folder)/"events.json"
        path.write_text(json.dumps(case["telemetry"]), encoding="utf-8")
        adapter = InvestigationTools(path, cfg.runbooks_file, case["resource"])
        if variant in {"react", "react_prefetch"}:
            session = InvestigationRunner(adapter, InvestigationModel(cfg), deadline_seconds=cfg.diagnosis_deadline,
                                          prefetch=variant == "react_prefetch").run(
                text=case["request"], owner="alice", binding="experiment")
        else:
            observations = collect_fixed(adapter, case["request"])
            metadata, error = {}, None
            try:
                if variant == "rules":
                    step = FrozenRules().finish(observations, case["resource"])
                else:
                    step, metadata = InvestigationModel(cfg).choose(case["request"], observations, 0, timeout=cfg.diagnosis_deadline)
                if step.kind != "finish":
                    raise ValueError("baseline must finish without further queries")
                validate_citations(step, observations)
            except Exception as exc:
                error = type(exc).__name__
                metadata = getattr(exc, "diagnostic_metadata", metadata)
                step = None
            session = {"assessment": step.assessment if step else "insufficient", "finding": step.runbook_id if step else None,
                "summary": step.summary if step else "调用或校验失败", "citations": [c.model_dump() for c in step.citations] if step else [],
                "tool_calls": len(observations), "model_calls": int(variant != "rules"),
                "source": "rule" if variant == "rules" else "deepseek", "observations": observations,
                "stop_reason": "model_finish" if not error else error, "decisions": [{"step": step.model_dump() if step else None, "metadata": metadata}]}
    selected = session.get("finding")
    refs = {c["record_id"] for c in session["citations"]}
    label_match = selected == case["expected_runbook"] and set(case["required_log_ids"]) <= refs
    correct = label_match and session["stop_reason"] == "model_finish"
    usages = [d["metadata"].get("usage", {}).get("total_tokens") for d in session["decisions"]]
    llm_calls = session["model_calls"] if session["source"] == "deepseek" else 0
    tokens = (sum(usages) if len(usages) == llm_calls and all(type(u) is int for u in usages) else None) if llm_calls else 0
    return {"id": case["id"], "split": case["split"], "resource": case["resource"], "variant": variant,
        "expected_runbook": case["expected_runbook"], "contract_passed": correct, "label_match": label_match,
        "semantic_passed": None, "human_review": None,
        "wrong_specific": selected is not None and not label_match, "elapsed_ms": round((time.perf_counter()-start)*1000,2),
        "llm_calls": llm_calls, "tokens": tokens, "session": session}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许真实 DeepSeek 调用，有 API 用量")
    parser.add_argument("--split", choices=["dev", "test", "branch", "all"], default="dev")
    parser.add_argument("--ids", nargs="+", help="定位复测子集；报告保留所选 ID，不与全量成绩合并")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategies", nargs="+", choices=["rules", "fixed_llm", "react", "react_prefetch"])
    args = parser.parse_args()
    cfg = get_settings()
    fixture = ROOT/"evals"/("it_investigation_branches.json" if args.split == "branch" else "it_investigation.json")
    cases = [c for c in json.loads(fixture.read_text(encoding="utf-8"))["cases"] if args.split == "all" or c["split"] == args.split]
    if args.ids:
        if set(args.ids) - {c["id"] for c in cases}:
            parser.error("unknown case IDs in selected split")
        cases = [c for c in cases if c["id"] in args.ids]
    variants = ["rules", "fixed_llm", "react"] if args.live else ["rules"]
    if args.strategies:
        if not args.live and any(v != "rules" for v in args.strategies):
            parser.error("model strategies require --live")
        variants = args.strategies
    if args.live and not cfg.deepseek_api_key.get_secret_value().strip():
        parser.error("DEEPSEEK_API_KEY required")
    schedule = [(c,v) for c in cases for v in variants]
    random.Random(17).shuffle(schedule)
    meta = {"split": args.split, "selected_ids": [c["id"] for c in cases], "live": args.live, "model": cfg.deepseek_model if args.live else None,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "runbooks_sha256": hashlib.sha256(cfg.runbooks_file.read_bytes()).hexdigest(),
        "source_sha256": {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), ROOT/"app/investigation.py")},
        "metric": "expected runbook selection plus required current log citation; not full semantic correctness of generated prose",
        "limitations": ["same author designed synthetic cases and baselines; not independent blinded annotation",
                        "finite regex baseline, not all possible rule programs", "one run per case, small sample",
                        "same source tools, owner scope, max 5 tool calls; fixed LLM sees deterministic targeted retrieval before its one completion",
                        "no enterprise system connection; all logs are authored simulations"]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x",encoding="utf-8") as output, args.output.with_suffix(".jsonl").open("x",encoding="utf-8") as journal:
        journal.write(json.dumps({"metadata":meta},ensure_ascii=False)+"\n"); journal.flush()
        rows=[]
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in as_completed([pool.submit(evaluate,c,v,cfg) for c,v in schedule]):
                row=future.result(); rows.append(row)
                journal.write(json.dumps(row,ensure_ascii=False)+"\n"); journal.flush()
                print(f"{len(rows)}/{len(schedule)} {row['id']} {row['variant']} pass={row['contract_passed']} tools={row['session']['tool_calls']} stop={row['session']['stop_reason']}",flush=True)
        summaries=[]
        for v in variants:
            group=[r for r in rows if r["variant"]==v]
            summaries.append({"variant":v,"n":len(group),"passed":sum(r["contract_passed"] for r in group),
                "wrong_specific":sum(r["wrong_specific"] for r in group),"tools":sum(r["session"]["tool_calls"] for r in group),
                "execution_failures":sum(r["session"]["stop_reason"]!="model_finish" for r in group),
                "llm_calls":sum(r["llm_calls"] for r in group),
                "tokens":sum(r["tokens"] for r in group) if all(r["tokens"] is not None for r in group) else None})
        json.dump({"metadata":meta,"summary":summaries,"records":rows},output,ensure_ascii=False,indent=2)
    print(json.dumps(summaries,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
