"""Create or score a blinded human review packet for a diagnostic evaluation report."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def build(report_path, inputs_path, output_dir):
    report_path, inputs_path, output_dir = Path(report_path), Path(inputs_path), Path(output_dir)
    if output_dir.exists():
        raise ValueError("review output directory must be new")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("mode") != "live_diagnostic_subflow":
        raise ValueError("human review requires a completed live diagnostic report")
    inputs = {row["id"]: row for row in
              (json.loads(line) for line in inputs_path.read_text(encoding="utf-8").splitlines() if line.strip())}
    records = list(report.get("records", []))
    if not records or any(row.get("id") not in inputs for row in records):
        raise ValueError("report records must match the frozen dataset")
    random.Random(digest(report_path)).shuffle(records)
    items, assignments = [], {}
    for index, row in enumerate(records, 1):
        blind_id = f"review-{index:04d}"
        case = inputs[row["id"]]
        session = row.get("session", {})
        assignments[blind_id] = {"case_id": row["id"], "scenario": None, "variant": None,
                                 "split": None, "contract_passed": row.get("contract_passed")}
        items.append({
            "blind_id": blind_id,
            "resource": case.get("resource"),
            "request": case.get("request"),
            "time_scope": session.get("time_scope"),
            "assessment": session.get("assessment"),
            "summary": session.get("summary"),
            "next_check": session.get("next_check"),
            "citations": session.get("citations", []),
            "observations": session.get("observations", []),
            "stop_reason": session.get("stop_reason"),
            "human_review": {"reviewer": "", "evidence_supported": None,
                             "procedure_applicable": None, "handoff_useful": None,
                             "uncertainty_preserved": None, "notes": ""},
        })
    packet = {"format": "diagnostic_human_review_v1", "evaluation_sha256": digest(report_path),
              "dataset_sha256": digest(inputs_path),
              "scope": "Model output and current evidence are shown without expected labels or scenario names; human fields are empty.",
              "items": items}
    mapping = {"format": "diagnostic_human_review_mapping_v1", "evaluation_sha256": digest(report_path),
               "dataset_sha256": digest(inputs_path), "assignments": assignments}
    output_dir.mkdir(parents=True)
    write_new(output_dir / "review-packet.json", packet)
    write_new(output_dir / "private-mapping.json", mapping)
    return {"items": len(items), "output": str(output_dir), "human_review_complete": False}


def score(packet_path, reviewed_path, mapping_path, output_path):
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    reviewed = json.loads(Path(reviewed_path).read_text(encoding="utf-8"))
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    if packet.get("evaluation_sha256") != reviewed.get("evaluation_sha256") or packet.get("evaluation_sha256") != mapping.get("evaluation_sha256"):
        raise ValueError("review provenance mismatch")
    originals = {row["blind_id"]: row for row in packet["items"]}
    rows = {row["blind_id"]: row for row in reviewed.get("items", [])}
    if set(originals) != set(rows) or set(originals) != set(mapping.get("assignments", {})):
        raise ValueError("review IDs missing, duplicated or changed")
    dimensions = ("evidence_supported", "procedure_applicable", "handoff_useful", "uncertainty_preserved")
    counts = Counter()
    completed = 0
    passed = 0
    for blind_id, original in originals.items():
        row = rows[blind_id]
        if {key: value for key, value in row.items() if key != "human_review"} != {key: value for key, value in original.items() if key != "human_review"}:
            raise ValueError("answer or evidence changed during review")
        review = row.get("human_review") or {}
        counts["total"] += 1
        if not review.get("reviewer", "").strip() or any(type(review.get(key)) is not bool for key in dimensions):
            continue
        if not review.get("notes", "").strip():
            raise ValueError("completed review requires notes")
        completed += 1
        counts.update({key: int(review[key]) for key in dimensions})
        passed += int(all(review[key] for key in dimensions))
    result = {"format": "diagnostic_human_review_score_v1", "packet_sha256": digest(packet_path),
              "review_sha256": digest(reviewed_path), "complete": completed == counts["total"],
              "total": counts["total"], "reviewed": completed, "passed": passed,
              "dimension_counts": {key: counts[key] for key in dimensions},
              "scope": "Human review of diagnostic usefulness; missing reviews are not passes; this is not an independent enterprise benchmark."}
    write_new(output_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prepare = subs.add_parser("prepare")
    prepare.add_argument("--report", type=Path, required=True)
    prepare.add_argument("--inputs", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    grade = subs.add_parser("score")
    grade.add_argument("--packet", type=Path, required=True)
    grade.add_argument("--reviewed", type=Path, required=True)
    grade.add_argument("--mapping", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (build(args.report, args.inputs, args.output_dir) if args.command == "prepare"
                  else score(args.packet, args.reviewed, args.mapping, args.output))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
