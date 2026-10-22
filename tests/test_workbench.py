import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.api import create_app
from app.audit import AuditStore
from app.config import get_settings
from app.errors import ConfigurationError
from app.knowledge import KnowledgeBase
from app.preflight import Preflight
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities,auth_headers


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.identities,self.keys=demo_identities(self.root)
        self.cfg=get_settings(_env_file=None,data_dir=self.root,llm_mode="offline",rag_backend="lexical",runbook_strategy="bm25")
        self.audit=AuditStore(self.cfg.db_path)
        self.agent=EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root/"kb",backend="lexical"),self.audit),self.audit,settings=self.cfg)

    def tearDown(self):self.temp.cleanup()

    def test_static_workbench_and_runtime_disclose_no_secrets(self):
        with TestClient(create_app(self.agent,identities=self.identities)) as client:
            response=client.get("/")
            self.assertEqual(response.status_code,200)
            self.assertIn("script-src 'self'",response.headers["content-security-policy"])
            self.assertIn("no-store",response.headers["cache-control"])
            self.assertEqual(client.get("/static/app.js").status_code,200)
            self.assertEqual(client.get("/runtime").status_code,401)
            data=client.get("/runtime",headers=auth_headers(self.keys,"alice")).json()
            self.assertEqual(data["model_mode"],"offline")
            self.assertNotIn("key",str(data))
            self.assertEqual(client.get("/static/../config.py").status_code,404)

    def test_preflight_offline_is_warning_and_errors_are_redacted(self):
        result=Preflight(self.cfg).run()
        self.assertEqual(next(x for x in result["checks"] if x["name"]=="model_mode")["status"],"warn")
        with patch("app.auth.IdentityStore",side_effect=ValueError("secret-body")):
            result=Preflight(self.cfg).run()
        self.assertFalse(result["passed"])
        self.assertNotIn("secret-body",str(result))

    def test_http_config_needs_explicit_secret_and_secure_remote_origin(self):
        for fields in [{"action_backend":"http_incidents"},{"action_base_url":"http://example.com"},
                       {"action_base_url":"https://example.com/?secret=bad"}]:
            with self.assertRaises(ConfigurationError):get_settings(_env_file=None,**fields)
        cfg=get_settings(_env_file=None,action_backend="http_incidents",action_api_key="t"*40)
        self.assertEqual(cfg.action_backend,"http_incidents")
