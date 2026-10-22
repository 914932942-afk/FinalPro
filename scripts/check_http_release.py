"""Real model + local vectors + separate HTTP downstream. Explicit --live spends model tokens."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error("use a new output file")
    with tempfile.TemporaryDirectory(prefix="http-release-") as folder:
        with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        token=secrets.token_urlsafe(32)
        env=dict(os.environ,DEMO_DOWNSTREAM_KEY=token,AGENT_ACTION_API_KEY=token,AGENT_ACTION_BACKEND="http_incidents",AGENT_ACTION_BASE_URL=f"http://127.0.0.1:{port}")
        server=subprocess.Popen([sys.executable,"-m","app.demo_downstream","--db",str(Path(folder)/"service.db"),"--port",str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if httpx.get(env["AGENT_ACTION_BASE_URL"]+"/health",timeout=.2,trust_env=False).status_code==200:break
                except httpx.HTTPError:pass
                if server.poll() is not None:raise RuntimeError("downstream startup failed")
                time.sleep(.05)
            else:raise RuntimeError("downstream startup timed out")
            subprocess.run([sys.executable,"-X","utf8","scripts/check_investigation_workflow.py","--live","--runbook-strategy","vector","--case-id","t04","--case-id","t07","--output",str(args.output.resolve())],cwd=ROOT,env=env,check=True)
        finally:
            server.terminate();server.wait(timeout=10)


if __name__=="__main__":main()
