"""启动真实 Uvicorn 进程，验证 HTTP 链路；不调用外部模型。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen
from auth_helpers import demo_identities, auth_headers


class ServerTests(unittest.TestCase):
    def test_live_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, keys = demo_identities(Path(tmp))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = {**os.environ, 'AGENT_DATA_DIR': tmp,
                   'AGENT_DB_PATH': str(Path(tmp) / 'agent.db'),
                   'AGENT_KB_DIR': str(Path(tmp) / 'knowledge'),
                   'AGENT_AUTH_FILE': str(Path(tmp) / 'auth.json'),
                   'AGENT_HOST': '127.0.0.1', 'AGENT_PORT': str(port),
                   'AGENT_LLM_MODE': 'offline', 'AGENT_RAG_BACKEND': 'lexical'}
            process = subprocess.Popen([sys.executable, '-m', 'app.api'], env=env,
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = f'http://127.0.0.1:{port}'
            try:
                for _ in range(80):
                    if process.poll() is not None:
                        self.fail('server exited before startup')
                    try:
                        with urlopen(base + '/health', timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail('startup timeout')
                body = json.dumps({'title': 'password reset', 'description': 'login failed'}).encode()
                def post():
                    request = Request(base + '/tickets', data=body,
                        headers={'Content-Type': 'application/json', 'Idempotency-Key': 'live', **auth_headers(keys)})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 201)
                        return json.load(response)
                first = post()
                self.assertEqual(first['ticket']['status'], 'closed')
                self.assertEqual(first, post())
            finally:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
