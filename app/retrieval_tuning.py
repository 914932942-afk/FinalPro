"""Bounded development ablation. Never tune on the later validation fixture."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .config import get_settings
from .knowledge import KnowledgeBase
from .knowledge_experiment import observed_environments
from .runbook_knowledge import RunbookKnowledge

ROOT = Path(__file__).resolve().parents[1]
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def document_views(records):
    """Remove repeated paragraphs only from the ranking view; retain source bytes."""
    parts = [[p.strip() for p in re.split(r"\n\s*\n", r["text"]) if p.strip()] for r in records]
    frequencies = Counter(p for paragraphs in parts for p in set(paragraphs))
    result = []
    for record, paragraphs in zip(records, parts):
        kept = [p for p in paragraphs if frequencies[p] == 1 and
                not re.fullmatch(r"#+\s+kb-[a-z]\d+\s+排障规程", p) and not p.startswith("## ")]
        result.append("\n\n".join(kept) or record["text"])
    return result


def lexical_ranking(query, texts, ids):
    terms = set(KnowledgeBase._terms(query))
    counts = [Counter(KnowledgeBase._terms(text)) for text in texts]
    lengths = [sum(c.values()) for c in counts]
    avg = sum(lengths)/len(lengths) if lengths else 1
    avg = avg or 1
    freq = {term:sum(term in c for c in counts) for term in terms}
    scores = [sum(math.log(1+(len(counts)-freq[t]+.5)/(freq[t]+.5))*c[t]*2.2 /
                  (c[t]+1.2*(.25+.75*length/avg)) for t in terms) for c,length in zip(counts,lengths)]
    return sorted((i for i,s in enumerate(scores) if s>0), key=lambda i:(-scores[i],ids[i]))


def fuse(lexical, dense, ids, rank_constant=60, anchor=False):
    if anchor:
        return list(dict.fromkeys(lexical[:1]+dense+lexical))
    scores = {}
    for ranked in (lexical,dense):
        for rank,i in enumerate(ranked,1):scores[i]=scores.get(i,0)+1/(rank_constant+rank)
    return sorted(scores,key=lambda i:(-scores[i],ids[i]))


def applicable_scope(records, environments):
    allowed={"any"}
    if len(set(environments))==1:allowed.update(environments)
    return [r for r in records if r.get("environment","any") in allowed]


def legacy_scope(records, environments):
    if len(set(environments))==1:
        return [r for r in records if r.get("environment","any") in {"any",*environments}]
    return list(records)


class LegacyRunbookKnowledge(RunbookKnowledge):
    """Immutable old baseline semantics for experiments, never a runtime default."""
    scope = staticmethod(legacy_scope)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.query_instruction = ""


# Each incremental candidate has a single explained difference. No exhaustive weight sweep.
CANDIDATES = {
    "old_bm25":dict(scope=False,view=False,instruction=False,method="bm25"),
    "old_hybrid":dict(scope=False,view=False,instruction=False,method="hybrid"),
    "scoped_bm25":dict(scope=True,view=False,instruction=False,method="bm25"),
    "scoped_hybrid":dict(scope=True,view=False,instruction=False,method="hybrid"),
    "scoped_view":dict(scope=True,view=True,instruction=False,method="hybrid"),
    "scoped_instruction":dict(scope=True,view=False,instruction=True,method="hybrid"),
    "scoped_instruction_vector":dict(scope=True,view=False,instruction=True,method="vector"),
    "scoped_instruction_rrf10":dict(scope=True,view=False,instruction=True,method="hybrid",rank_constant=10),
    "scoped_instruction_anchor":dict(scope=True,view=False,instruction=True,method="hybrid",anchor=True),
    "scoped_both":dict(scope=True,view=True,instruction=True,method="hybrid"),
    "scoped_both_rrf10":dict(scope=True,view=True,instruction=True,method="hybrid",rank_constant=10),
    "scoped_both_anchor":dict(scope=True,view=True,instruction=True,method="hybrid",anchor=True),
    "scoped_both_vector":dict(scope=True,view=True,instruction=True,method="vector"),
}


def run(fixture):
    cfg=get_settings(_env_file=None)
    kb=RunbookKnowledge(cfg.runbooks_dir,settings=cfg)
    cases=json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    documents={resource:kb.capture(resource) for resource in {c["resource"] for c in cases}}
    views={r["id"]:view for rows in documents.values() for r,view in zip(rows,document_views(rows))}
    texts={r["text"] for rows in documents.values() for r in rows}|set(views.values())
    queries={c["query"]+(" "+observed_environments(c)[0] if len(observed_environments(c))==1 else "") for c in cases}
    texts=sorted(texts|queries|{QUERY_INSTRUCTION+q for q in queries})
    started=time.perf_counter()
    vectors=dict(zip(texts,kb.kb._encode(texts)))
    results=[]
    for name,option in CANDIDATES.items():
        records=[]
        for case in cases:
            env=observed_environments(case);original=documents[case["resource"]]
            docs=(applicable_scope(original,env) if option["scope"] else legacy_scope(original,env))
            query=case["query"]+(" "+env[0] if len(env)==1 else "")
            ids=[d["id"] for d in docs]
            search_texts=[views[d["id"]] if option["view"] else d["text"] for d in docs]
            lex=lexical_ranking(query,search_texts,ids)
            q=vectors[(QUERY_INSTRUCTION if option["instruction"] else "")+query]
            scores=[sum(a*b for a,b in zip(q,vectors[t])) for t in search_texts]
            dense=sorted(range(len(docs)),key=lambda i:(-scores[i],ids[i]))
            order=(lex if option["method"]=="bm25" else dense if option["method"]=="vector" else
                   fuse(lex,dense,ids,option.get("rank_constant",60),option.get("anchor",False)))
            expected=case["expected_runbook"]
            ranked=[ids[i] for i in order]
            rank=ranked.index(expected)+1 if expected in ranked else None
            records.append({"id":case["id"],"group":case["group"],"ranked":ranked,"expected":expected,
                            "recall2":expected in ranked[:2] if expected else None,"rr":1/rank if rank else 0})
        positive=[r for r in records if r["expected"]]
        results.append({"candidate":name,"options":option,"recall2":sum(r["recall2"] for r in positive),
            "answerable_n":len(positive),"mrr":sum(r["rr"] for r in positive)/len(positive),"records":records})
    return {"scope":"Development only: existing 32-case probe has been seen. Bounded predefined ablations, no held-out results used.",
        "fixture_sha256":hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "corpus_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in cfg.runbooks_dir.glob('*.md')},
        "selection_rule":"Choose highest development recall@2, then MRR, then simpler candidate. Validate chosen candidate once on new cases; do not retune on validation.",
        "elapsed_seconds":time.perf_counter()-started,"results":results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,default=ROOT/"evals/retrieval_probe.json")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    with args.output.open("x",encoding="utf-8") as stream:
        result=run(args.fixture);json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps([{k:r[k] for k in ('candidate','recall2','answerable_n','mrr')} for r in result['results']]),flush=True)


if __name__=="__main__":main()
