"""Independent persistent demo service. No connection to enterprise IAM or ticket systems."""
import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, ConfigDict, Field

class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=100)
    resource: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10000)
    severity: str = Field(max_length=20)

class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: str = Field(min_length=1, max_length=100)
    report: dict

class IncidentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents(ticket_id TEXT PRIMARY KEY, id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attachments(ticket_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(self, action, key, payload):
        content = json.dumps({"action": action, "payload": payload}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint,payload FROM receipts WHERE key=?", (key,)).fetchone()
            if old:
                if old[0] != digest:
                    raise HTTPException(409, "idempotency_conflict")
                return json.loads(old[1])
            ticket_id = payload["ticket_id"]
            row = db.execute("SELECT id FROM incidents WHERE ticket_id=?", (ticket_id,)).fetchone()
            if action == "open_incident":
                if row:
                    raise HTTPException(409, "ticket_already_registered_with_another_key")
                downstream_id = "D-" + uuid5(NAMESPACE_URL, ticket_id).hex
                db.execute("INSERT INTO incidents VALUES(?,?,?)", (ticket_id, downstream_id, content))
            else:
                if not row:
                    raise HTTPException(404, "incident_missing")
                if db.execute("SELECT 1 FROM attachments WHERE ticket_id=?", (ticket_id,)).fetchone():
                    raise HTTPException(409, "attachment_already_written")
                downstream_id = row[0]
                db.execute("INSERT INTO attachments VALUES(?,?)", (ticket_id, content))
            receipt = dict(action=action, status="succeeded", idempotency_key=key,
                           ticket_id=ticket_id, downstream_id=downstream_id, simulated=True)
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (key, digest, json.dumps(receipt)))
            return receipt

    def lookup(self, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM receipts WHERE key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "receipt_missing")
        return json.loads(row[0])

def create_app(path, api_key, *, after_commit_delay=0, reject_attachments=False):
    if len(api_key) < 32:
        raise ValueError("demo service requires an independent 32-character key")
    store = IncidentStore(path)
    app = FastAPI(title="Independent demo incident service")
    app.state.store = store

    def authorized(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + api_key).encode()):
            raise HTTPException(401, "unauthenticated")

    def write(action, payload, key):
        result = store.write(action, key, payload)

        if after_commit_delay:
            time.sleep(after_commit_delay)
        return result

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "demo_incidents", "simulated": True}

    @app.get("/ready", dependencies=[Depends(authorized)])
    def ready():
        with store.connect() as db:
            db.execute("SELECT 1 FROM receipts LIMIT 1")
        return {"status": "ok", "simulated": True}

    @app.post("/incidents", status_code=201, dependencies=[Depends(authorized)])
    def incident(body: Incident, idempotency_key: str = Header(min_length=1, max_length=256)):
        return write("open_incident", body.model_dump(), idempotency_key)

    @app.post("/attachments", dependencies=[Depends(authorized)])
    def attachment(body: Attachment, idempotency_key: str = Header(min_length=1, max_length=256)):
        if reject_attachments:
            raise HTTPException(503, "test_attachment_unavailable")
        return write("attach_evidence", body.model_dump(), idempotency_key)

    @app.get("/operations/{key:path}", dependencies=[Depends(authorized)])
    def lookup(key: str):
        return store.lookup(key)

    return app

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/demo_service.db"))
    parser.add_argument("--port", type=int, default=8091)

    parser.add_argument("--test-after-commit-delay", type=float, default=0)
    parser.add_argument("--test-reject-attachments", action="store_true")
    args = parser.parse_args()
    from .config import get_settings
    key = os.environ.get("DEMO_DOWNSTREAM_KEY") or get_settings().action_api_key.get_secret_value()
    import uvicorn
    uvicorn.run(create_app(args.db, key, after_commit_delay=args.test_after_commit_delay,
                          reject_attachments=args.test_reject_attachments), host="127.0.0.1", port=args.port,
                log_level="warning", access_log=False)

if __name__ == "__main__":
    main()
