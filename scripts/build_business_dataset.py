"""Build a new synthetic release or collect a separate pinned public log snapshot."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.scenario_dataset import PublicLogCollector, ScenarioDatasetBuilder
from app.public_incidents import PublicIncidentCollector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "collect-public", "collect-loghub", "collect-uci"])
    parser.add_argument("--output", type=Path, default=ROOT / "evals/business_scenarios_v1")
    parser.add_argument("--uci-public-http", action="store_true", help="Explicitly use UCI's public HTTP data endpoint if its HTTPS certificate is unavailable")
    args = parser.parse_args()
    if args.command == "build":
        result = ScenarioDatasetBuilder().write(args.output, ROOT / "data/knowledge/runbooks")
        print(json.dumps({"output": str(args.output), "cases": result["cases"], "synthetic": True}, ensure_ascii=False))
    else:
        if not (args.output / "manifest.json").is_file():
            parser.error("build the synthetic release before collecting its separate public supplement")
        if args.command in {"collect-public", "collect-loghub"}:
            result = PublicLogCollector().collect(args.output / "third_party/loghub")
            print(json.dumps({"source": result["source"], "commit": result["commit"],
                              "raw_log_lines": sum(r["lines"] for r in result["files"] if r["path"].endswith(".log"))}, ensure_ascii=False))
        if args.command in {"collect-public", "collect-uci"}:
            result = PublicIncidentCollector().collect(args.output / "third_party/uci498", public_http=args.uci_public_http)
            print(json.dumps({"source": result["source"], "events": result["events"], "incidents": result["incidents"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
