"""Readiness checks without business writes. --live explicitly makes a small paid model call."""
import argparse
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from .config import get_settings


class Preflight:
    def __init__(self, settings):
        self.settings = settings
        self.checks = []

    def check(self, name, fn):
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = "fail", type(exc).__name__
        self.checks.append(dict(name=name, status=status, detail=detail))

    def run(self, *, embedding=False, live=False):
        c = self.settings
        from .auth import IdentityStore
        from .runbook_knowledge import RunbookKnowledge
        self.check("identity", lambda: ("pass", f"{len(IdentityStore(c.auth_file)._records)} configured identities; keys omitted"))
        def database():
            if not c.db_path.exists():
                return "warn", "database not initialized; first service startup creates it"
            with closing(sqlite3.connect(c.db_path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
                valid = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return ("pass", "database readable and structurally valid") if valid and {"tickets","states","operations"} <= tables else ("fail", "database structure invalid")
        self.check("database", database)
        def knowledge():
            kb = RunbookKnowledge(c.runbooks_dir, settings=c)
            rows = kb.capture("vpn") + kb.capture("erp")
            if not rows or kb.conflicts(rows):
                return "fail", "runbooks missing or active family/environment conflicts"
            return "pass", f"{len(rows)} active runbooks loaded"
        self.check("runbooks", knowledge)
        self.check("model_mode", lambda: ("warn", "offline rules; not real model evidence") if c.llm_mode == "offline" or not c.deepseek_api_key.get_secret_value() else ("pass", "real model configured; connectivity checked only with --live"))
        if embedding:
            def encode():
                kb = RunbookKnowledge(c.runbooks_dir, settings=c)
                docs, result = kb.search("当前 VPN 域名解析失败", kb.capture("vpn"))
                return ("fail", "vector unavailable; explicit BM25 fallback: "+result.get("reason", "unknown")) if result.get("degraded") else ("pass", "retrieval strategy="+result["strategy"])
            self.check("embedding", encode)
        else:
            self.checks.append(dict(name="embedding", status="skipped", detail="use --embedding to load local weights"))
        if c.action_backend == "http_incidents":
            from .http_actions import IncidentHTTPAdapter
            adapter = IncidentHTTPAdapter(c.action_base_url, c.action_api_key.get_secret_value(), timeout=c.action_timeout)
            def downstream():
                data = adapter._request("GET", "/ready")
                return ("pass", "authenticated demo service ready; business remains simulated") if data == {"status":"ok","simulated":True} else ("fail", "unexpected downstream contract")
            self.check("downstream", downstream)
        else:
            self.checks.append(dict(name="downstream", status="warn", detail="in-process simulated adapter; no outbound HTTP"))
        if live:
            def model():
                if c.llm_mode == "offline" or not c.deepseek_api_key.get_secret_value():
                    return "fail", "live check requires enabled model credentials"
                from .model_adapter import DeepSeekDecisionModel
                client = DeepSeekDecisionModel(c.deepseek_api_key.get_secret_value(), base_url=c.deepseek_base_url,
                    model=c.deepseek_model, timeout=min(c.deepseek_timeout, 15), max_retries=0)
                response = client._post_json("/chat/completions", {"model":c.deepseek_model,
                    "messages":[{"role":"user","content":'Return exactly JSON: {"ok":true}'}],
                    "response_format":{"type":"json_object"}, "thinking":{"type":"disabled"},
                    "max_tokens":32,"temperature":0})
                if json.loads(response["choices"][0]["message"]["content"]) != {"ok":True}:
                    return "fail", "structured output failed"
                return "pass", {"structured_response":True,"usage":client._safe_usage(response.get("usage"))}
            self.check("live_model", model)
        return {"passed":not any(x["status"]=="fail" for x in self.checks),
                "scope":"readiness only, no business actions; warnings and skipped checks are not verification", "checks":self.checks}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding",action="store_true")
    parser.add_argument("--live",action="store_true",help="small paid structured generation, no business writes")
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.output and args.output.exists():parser.error("preserve earlier output; choose a new filename")
    try: result=Preflight(get_settings()).run(embedding=args.embedding,live=args.live)
    except Exception as exc:result={"passed":False,"checks":[{"name":"configuration","status":"fail","detail":type(exc).__name__}]}
    text=json.dumps(result,ensure_ascii=False,indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open("x",encoding="utf-8") as f:f.write(text)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":main()
