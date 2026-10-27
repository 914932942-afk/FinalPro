"""Start the workbench and its independent demo HTTP service with one command."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import webbrowser

from .auth import initialize_demo_identities, initialize_local_passwords
from .config import get_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8080)
    parser.add_argument("--service-port",type=int,default=8091)
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args()
    if args.port==args.service_port or not all(1<=p<=65535 for p in (args.port,args.service_port)):
        parser.error("choose two different valid ports")
    for port in (args.port,args.service_port):
        try:
            with socket.socket() as probe:probe.bind(("127.0.0.1",port))
        except OSError:parser.error(f"port {port} already used; existing processes are not stopped")
    cfg=get_settings()
    if not cfg.auth_file.exists():initialize_demo_identities(cfg.auth_file)
    accounts_path = initialize_local_passwords(cfg.auth_file)
    from .business_accounts import initialize_demo_bindings
    initialize_demo_bindings(cfg.account_bindings_file, cfg.auth_file)
    cfg.data_dir.mkdir(parents=True,exist_ok=True)
    token_file=cfg.data_dir/"demo_service.key"
    if not token_file.exists():
        with token_file.open("x",encoding="utf-8") as f:f.write(secrets.token_urlsafe(32))
    token=token_file.read_text(encoding="utf-8").strip()
    if len(token)<32:parser.error("invalid demo service key file")
    env=dict(os.environ,AGENT_ACTION_BACKEND="http_incidents",AGENT_ACTION_BASE_URL=f"http://127.0.0.1:{args.service_port}",
             AGENT_ACTION_API_KEY=token,DEMO_DOWNSTREAM_KEY=token,AGENT_HOST="127.0.0.1",AGENT_PORT=str(args.port))
    root=Path(__file__).resolve().parents[1]
    children=[]
    try:
        children.append(subprocess.Popen([sys.executable,"-m","app.demo_downstream","--db",str(cfg.data_dir/"demo_service.db"),"--port",str(args.service_port)],cwd=root,env=env))
        import httpx
        for _ in range(100):
            if children[0].poll() is not None:raise RuntimeError("downstream exited")
            try:
                if httpx.get(env["AGENT_ACTION_BASE_URL"]+"/health",timeout=.3,trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        else:raise RuntimeError("downstream startup timed out")
        children.append(subprocess.Popen([sys.executable,"-m","app.api"],cwd=root,env=env))
        url=f"http://127.0.0.1:{args.port}"
        for _ in range(100):
            if children[-1].poll() is not None:raise RuntimeError("workbench exited")
            try:
                if httpx.get(url+"/health",timeout=.3,trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.1)
        else:raise RuntimeError("workbench startup timed out")
        print(f"Workbench: {url}\nLocal account instructions: {accounts_path}\nBusiness and logs remain simulated. Press Ctrl+C to stop both child processes.",flush=True)
        if not args.no_browser:webbrowser.open(url)
        while all(p.poll() is None for p in children):time.sleep(.3)
    except KeyboardInterrupt:pass
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


if __name__=="__main__":main()
