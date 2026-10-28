"""Reference read-only query service backed by explicitly simulated replay events."""
import argparse
from collections import OrderedDict
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from threading import RLock
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .business_accounts import AccountDirectory, DiagnosticDataGap
from .event_query import IncidentWindow


class EventQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    resource: str = Field(pattern=r"^(vpn|erp)$")
    account_id: str = Field(min_length=1, max_length=200)
    start_at: str = Field(max_length=50)
    end_at: str = Field(max_length=50)
    page_size: int = Field(default=100, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=200)
    snapshot_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def bounded(self):
        IncidentWindow(self.start_at, self.end_at, "service_request")
        if bool(self.cursor) != bool(self.snapshot_id):
            raise ValueError("cursor and snapshot must be supplied together")
        return self


class EmptyKnowledge:
    @staticmethod
    def capture(resource):
        return []


def create_event_app(events_path, bindings_path, api_key, *, source_timezone="+08:00", ttl_seconds=300, clock=time.monotonic):
    if len(api_key) < 32:
        raise ValueError("read-only source requires its own service credential")
    from .investigation import InvestigationTools
    application = FastAPI(title="Read-only replay event source")
    directory = AccountDirectory(bindings_path)
    cache, cursors, lock = OrderedDict(), {}, RLock()

    @application.get("/health")
    def health():
        return {"status": "ok", "read_only": True, "simulated": True}

    @application.post("/v1/events/query")
    def query(body: EventQueryRequest, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + api_key):
            raise HTTPException(403, "read-only source authorization required")
        scope = directory.resolve(body.owner_id, body.resource)
        try:
            directory.validate(scope)
        except DiagnosticDataGap:
            raise HTTPException(403, "account scope unavailable") from None
        if scope["account_id"] != body.account_id:
            raise HTTPException(403, "account scope mismatch")
        signature = (body.owner_id, body.resource, body.account_id, body.start_at, body.end_at, body.page_size, scope["revision"])
        with lock:
            expired = {key for key, value in cache.items() if clock() >= value["expires"]}
            for key in expired:
                cache.pop(key)
            for token in [token for token, (key, _) in cursors.items() if key not in cache]:
                cursors.pop(token)
            if body.cursor:
                position = cursors.get(body.cursor)
                if not position or position[0] != body.snapshot_id or body.snapshot_id not in cache:
                    raise HTTPException(410, "source snapshot expired")
                snapshot = cache[body.snapshot_id]
                if snapshot["signature"] != signature:
                    raise HTTPException(403, "continuation scope mismatch")
                offset = position[1]
            else:
                tools = InvestigationTools(events_path, events_path, body.resource, knowledge=EmptyKnowledge(),
                    accounts=directory, source_timezone=source_timezone)
                time_scope = {"status": "bounded", "window": IncidentWindow(body.start_at, body.end_at, "source_query").to_dict()}
                captured = tools.capture(body.owner_id, expected_scope=scope, time_scope=time_scope)
                try:
                    tools.validate_snapshot(captured, body.owner_id)
                except DiagnosticDataGap as exc:
                    # Preserve the distinction between a valid empty window and
                    # an unreadable/malformed source through an explicit gap.
                    raise HTTPException(422, {"gap_code": exc.code}) from None
                account = captured["account"]
                flattened = [("client", None, row) for row in account["client_events"]]
                flattened.extend(("session", ref, row) for ref, values in account["sessions"].items() for row in values)
                if len(flattened) > 500:
                    raise HTTPException(413, "narrow the event query window")
                ident = captured["snapshot_id"]
                snapshot = {"signature": signature, "rows": flattened, "expires": clock()+ttl_seconds,
                            "source": captured["source"], "snapshot_id": ident}
                cache[ident] = snapshot
                cache.move_to_end(ident)
                while len(cache) > 64:
                    cache.popitem(last=False)
                offset = 0
            page = {"owner_id": body.owner_id, "client_events": [], "sessions": {}}
            for kind, ref, original in snapshot["rows"][offset:offset+body.page_size]:
                row = {**original, "owner_id": body.owner_id, "account_id": body.account_id}
                if kind == "client":
                    page["client_events"].append(row)
                else:
                    page["sessions"].setdefault(ref, []).append(row)
            next_cursor = None
            if offset+body.page_size < len(snapshot["rows"]):
                next_cursor = secrets.token_urlsafe(24)
                cursors[next_cursor] = (snapshot["snapshot_id"], offset+body.page_size)
                # A bounded source must also bound replayed continuation tokens.
                if len(cursors) > 1000:
                    cursors.pop(next(iter(cursors)))
            return {"schema_version": 1, "owner_id": body.owner_id, "resource": body.resource,
                    "account_id": body.account_id, "start_at": body.start_at, "end_at": body.end_at,
                    "snapshot_id": snapshot["snapshot_id"], "source_id": "reference-simulated-event-service",
                    "timezone": snapshot["source"]["timezone"], "simulated": True, "truncated": False,
                    "account": page, "next_cursor": next_cursor}

    return application


def main():
    from .config import get_settings
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    cfg = get_settings()
    key = cfg.telemetry_api_key.get_secret_value()
    if args.key_file:
        key = args.key_file.read_text(encoding="utf-8").strip()
    application = create_event_app(cfg.investigation_file, cfg.account_bindings_file, key, source_timezone=cfg.diagnosis_timezone)
    uvicorn.run(application, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
