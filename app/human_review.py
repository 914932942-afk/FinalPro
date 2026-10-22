"""Freeze a dataset and prepare variant-blinded human review. Never fabricate independent labels."""
import argparse
import hashlib
import json
from pathlib import Path
import random
from collections import defaultdict
from .config import get_settings
from .runbook_knowledge import RunbookKnowledge


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)


class ReviewPipeline:
    def __init__(self, settings):
        self.settings=settings

    def freeze(self, fixture, author, *, independent=False):
        data=json.loads(Path(fixture).read_text(encoding="utf-8"))
        cases=data["cases"]
        if not cases or len({x["id"] for x in cases})!=len(cases):
            raise ValueError("nonempty cases with unique IDs required")
        kb=RunbookKnowledge(self.settings.runbooks_dir,settings=self.settings)
        for c in cases:
            if c.get("resource") not in {"vpn","erp"} or not c.get("query") or not c.get("request") or not c.get("logs"):
                raise ValueError("case needs resource, query, request and logs")
            if "expected_runbook" not in c or any(not r.get("id") or not r.get("text") for r in c["logs"]):
                raise ValueError("labels and identified evidence required")
            target=c["expected_runbook"]
            if target is not None and target not in {r["id"] for r in kb.capture(c["resource"])}:
                raise ValueError("target must be an active runbook for this resource")
        return {"format":"review_freeze_v1","author":author,"independent_author_declared":independent,
                "independence_verified":False,"source_sha256":digest(fixture),"case_count":len(cases),
                "fixture":data,"scope":"Author independence is a declaration requiring external verification, not a generated property.",
                "corpus_sha256":{p.name:digest(p) for p in sorted(self.settings.runbooks_dir.glob("*.md"))}}

    def packet(self, frozen, evaluation):
        frozen_data=json.loads(Path(frozen).read_text(encoding="utf-8"))
        current={p.name:digest(p) for p in sorted(self.settings.runbooks_dir.glob("*.md"))}
        if current!=frozen_data["corpus_sha256"]:raise ValueError("corpus changed after freeze")
        evaluated=json.loads(Path(evaluation).read_text(encoding="utf-8"))
        if evaluated.get("fixture_sha256")!=frozen_data["source_sha256"]:
            raise ValueError("evaluation fixture differs from frozen dataset")
        cases={c["id"]:c for c in frozen_data["fixture"]["cases"]}
        kb=RunbookKnowledge(self.settings.runbooks_dir,settings=self.settings)
        documents={r["id"]:r for resource in ("vpn","erp") for r in kb.capture(resource)}
        if evaluated.get("corpus_sha256") != current:raise ValueError("evaluation corpus differs from freeze")
        rows=list(evaluated["records"])
        random.Random(digest(evaluation)).shuffle(rows)
        packet=[];mapping={}
        for i,r in enumerate(rows,1):
            c=cases[r["id"]];blind_id=f"review-{i:04d}"
            mapping[blind_id]={"variant":r["variant"],"case_id":r["id"],"repeat":r.get("repeat",0)}
            docs=[documents[x] for x in r.get("retrieved_ids",[]) if x in documents]
            expected=c["expected_runbook"]
            packet.append({"blind_id":blind_id,"request":c["request"],"logs":c["logs"],
                "provided_runbooks":docs,"reference_runbook":documents.get(expected),
                "required_topics":c.get("required_topics",[]),"forbidden_claims":c.get("forbidden_claims",[]),
                "answer":r.get("answer"),"generation_missing":not bool(r.get("answer")),
                "human_review":{"reviewer":"","evidence_supported":None,"procedure_applicable":None,
                    "handoff_useful":None,"uncertainty_preserved":None,"notes":""}})
        return {"format":"human_review_v1","evaluation_sha256":digest(evaluation),
                "scope":"Variant labels and model judgments hidden; document content can still suggest a retrieval variant. Human reviews remain empty.",
                "items":packet}, {"evaluation_sha256":digest(evaluation),"assignments":mapping}

    @staticmethod
    def score(packet, reviewed, mapping):
        original=json.loads(Path(packet).read_text(encoding="utf-8"))
        reviews=json.loads(Path(reviewed).read_text(encoding="utf-8"))
        assignment=json.loads(Path(mapping).read_text(encoding="utf-8"))
        if original["evaluation_sha256"]!=assignment["evaluation_sha256"] or original["evaluation_sha256"]!=reviews["evaluation_sha256"]:
            raise ValueError("review provenance mismatch")
        originals={r["blind_id"]:r for r in original["items"]}
        if len(reviews["items"])!=len(originals) or {r["blind_id"] for r in reviews["items"]}!=set(originals):
            raise ValueError("review IDs changed, missing or duplicated")
        groups=defaultdict(lambda:{"total":0,"reviewed":0,"passed":0})
        for row in reviews["items"]:
            prior=originals[row["blind_id"]]
            if {k:v for k,v in row.items() if k!="human_review"}!={k:v for k,v in prior.items() if k!="human_review"}:
                raise ValueError("answers or evidence changed during review")
            g=groups[assignment["assignments"][row["blind_id"]]["variant"]];g["total"]+=1
            r=row["human_review"]
            scores=[r.get(k) for k in ["evidence_supported","procedure_applicable","handoff_useful","uncertainty_preserved"]]
            if not r.get("reviewer","").strip() or any(type(v) is not bool for v in scores):continue
            if not r.get("notes","").strip():raise ValueError("completed review requires justification")
            g["reviewed"]+=1;g["passed"]+=int(all(scores) and not row["generation_missing"])
        return {"scope":"Human review only; missing reviews are not passes. No claims of independent enterprise evaluation.",
                "complete":all(g["total"]==g["reviewed"] for g in groups.values()),"summary":dict(groups),
                "review_sha256":digest(reviewed)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest="command",required=True)
    freeze=subs.add_parser("freeze");freeze.add_argument("--fixture",type=Path,required=True);freeze.add_argument("--author",required=True)
    freeze.add_argument("--independent-author-declared",action="store_true");freeze.add_argument("--output",type=Path,required=True)
    packet=subs.add_parser("packet");packet.add_argument("--frozen",type=Path,required=True);packet.add_argument("--evaluation",type=Path,required=True);packet.add_argument("--output-dir",type=Path,required=True)
    score=subs.add_parser("score");score.add_argument("--packet",type=Path,required=True);score.add_argument("--reviewed",type=Path,required=True);score.add_argument("--mapping",type=Path,required=True);score.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();pipeline=ReviewPipeline(get_settings())
    if args.command=="freeze":write_new(args.output,pipeline.freeze(args.fixture,args.author,independent=args.independent_author_declared))
    elif args.command=="packet":
        if args.output_dir.exists():parser.error("output directory must be new")
        review,mapping=pipeline.packet(args.frozen,args.evaluation)
        write_new(args.output_dir/"review-packet.json",review)
        write_new(args.output_dir/"private-mapping.json",mapping)
    else:write_new(args.output,pipeline.score(args.packet,args.reviewed,args.mapping))


if __name__=="__main__":main()
