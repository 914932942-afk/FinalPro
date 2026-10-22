"""真实索引更新的受控验证；仅修改临时知识库，调用两次真实模型。"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.rag_evaluation import RagExperiment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a new output filename; previous evidence is preserved")
    os.environ["HF_HUB_OFFLINE"] = "1"
    case = next(c for c in json.loads((ROOT / "evals/rag_grounding.json").read_text(encoding="utf-8"))["cases"]
                if c["id"] == "dev_vpn_e417")
    code_hash = {name: hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
                 for name in ("model_adapter.py", "knowledge.py", "planning.py")}
    with tempfile.TemporaryDirectory() as temp:
        experiment = RagExperiment(temp, live=True)
        try:
            before = experiment.run(case, "hybrid")
            old_ids = {d["id"] for d in experiment.kb.documents if d["source"] == "local/vpn_e417.md"}
            path = experiment.cfg.kb_dir / "local/vpn_e417.md"
            old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_text('<!-- rag: {"resource":"vpn","codes":"E417","status":"active","version":"demo-update-v2"} -->\n'
                '# VPN E417 新版受控验证规程\n\n'
                '此内容仅用于临时知识库的模拟规则变更测试，不是真实厂商定义。\n'
                '适用范围：VPN 报错 E417。根据本次模拟变更，该错误需要人工核验网关状态。\n'
                '处理结论：manual_review。停止自动处理，不登记普通故障、不申请权限、不创建密码重置任务。\n', encoding="utf-8")
            experiment.kb.reindex()
            updated_case = copy.deepcopy(case)
            updated_case["expected"] = dict(next_step="manual_review", action=None, resource=None,
                                            permission=None, missing_fact=None)
            after = experiment.run(updated_case, "hybrid")
            stored_ids = set(experiment.kb.collection.get(include=[])["ids"])
            record = {"scope": "真实 BGE/Chroma 索引的模拟规则更新；相同工单、模型、参数和程序；只修改临时知识库，不是独立准确率样本。",
                "model": experiment.cfg.deepseek_model, "code_sha256": code_hash,
                "same_code": all(hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest() == value for name, value in code_hash.items()),
                "input": {key: case[key] for key in ("title", "description")},
                "before_document_sha256": old_hash, "after_document_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "old_chunks_removed": not bool(old_ids.intersection(stored_ids)),
                "changed_citation": bool(old_ids) and not bool(old_ids.intersection((after.get("decision") or {}).get("sop_refs", []))),
                "before": before, "after": after}
            record["passed"] = all([before.get("passed"), after.get("passed"), record["same_code"], record["old_chunks_removed"], record["changed_citation"]])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: record[key] for key in ("passed", "same_code", "old_chunks_removed", "changed_citation")}, indent=2))
        finally:
            experiment.close()


if __name__ == "__main__":
    main()
