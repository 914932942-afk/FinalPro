"""Read-only HTTP event source. Identity/window come exclusively from the backend."""
import hashlib
import json
import time

import httpx

from .business_accounts import DiagnosticDataGap
from .event_query import IncidentWindow


class HTTPEventSource:
    requires_window = True

    def __init__(self, base_url, api_key, *, timeout=5, max_pages=5, transport=None):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.timeout, self.max_pages, self.transport = timeout, max_pages, transport

    def capture(self, scope, window, *, timeout):
        if not window or not scope or scope.get("code") not in {"ready", "business_account_disabled"}:
            raise DiagnosticDataGap("account_binding_changed")
        window = IncidentWindow(**window)
        body = {"owner_id": scope["owner_id"], "resource": scope["resource"], "account_id": scope["account_id"],
                "start_at": window.start_at, "end_at": window.end_at, "page_size": 100,
                "cursor": None, "snapshot_id": None}
        account = {"owner_id": scope["owner_id"], "client_events": [], "sessions": {}}
        seen, revision, provenance = set(), None, None
        deadline = time.monotonic()+min(timeout, self.timeout)
        with httpx.Client(transport=self.transport, follow_redirects=False, trust_env=False) as client:
            for _ in range(self.max_pages):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise DiagnosticDataGap("source_unavailable")
                try:
                    with client.stream("POST", self.base_url + "/v1/events/query", json=body,
                                       headers={"Authorization": "Bearer " + self.api_key}, timeout=remaining) as response:
                        if response.status_code in {401, 403}:
                            raise DiagnosticDataGap("source_forbidden")
                        if response.status_code == 404:
                            raise DiagnosticDataGap("account_not_found")
                        if response.status_code == 410:
                            raise DiagnosticDataGap("source_snapshot_expired")
                        if response.status_code == 413:
                            raise DiagnosticDataGap("source_truncated")
                        if response.status_code == 422:
                            try:
                                detail = response.read()
                                if len(detail) > 4096:
                                    raise ValueError()
                                code = json.loads(detail)["detail"]["gap_code"]
                                from .business_accounts import DATA_GAPS
                                if code not in DATA_GAPS:
                                    raise ValueError()
                            except (ValueError, KeyError, TypeError):
                                raise DiagnosticDataGap("source_invalid") from None
                            raise DiagnosticDataGap(code)
                        if response.status_code != 200:
                            raise DiagnosticDataGap("source_unavailable")
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 262144:
                                raise DiagnosticDataGap("source_invalid")
                    page = json.loads(raw)
                except (httpx.HTTPError, OSError):
                    raise DiagnosticDataGap("source_unavailable") from None
                except (ValueError, TypeError):
                    raise DiagnosticDataGap("source_invalid") from None
                try:
                    if (not isinstance(page, dict) or page.get("schema_version") != 1
                            or any(page.get(key) != body[key] for key in ("owner_id", "resource", "account_id", "start_at", "end_at"))
                            or not isinstance(page.get("snapshot_id"), str) or not 1 <= len(page["snapshot_id"]) <= 100
                            or not isinstance(page.get("source_id"), str) or not 1 <= len(page["source_id"]) <= 100
                            or type(page.get("simulated")) is not bool or page.get("truncated") is not False):
                        raise ValueError("invalid source envelope")
                    if revision is not None and page["snapshot_id"] != revision:
                        raise ValueError("snapshot changed between pages")
                    revision = page["snapshot_id"]
                    current_provenance = {"source_id": page["source_id"], "snapshot_id": revision, "simulated": page["simulated"],
                                          "timezone": page.get("timezone"), "transport": "http_read_only"}
                    if provenance is not None and provenance != current_provenance:
                        raise ValueError("source provenance changed")
                    provenance = current_provenance
                    rows = page["account"]
                    if (rows.get("owner_id") != scope["owner_id"] or not isinstance(rows.get("client_events"), list)
                            or not isinstance(rows.get("sessions"), dict) or any(not isinstance(v, list) for v in rows["sessions"].values())):
                        raise ValueError("invalid account page")
                    records = rows["client_events"] + [r for values in rows["sessions"].values() for r in values]
                    if len(records) > 100:
                        raise ValueError("oversized event page")
                    for record in records:
                        if record.get("owner_id") != scope["owner_id"] or record.get("account_id") != scope["account_id"]:
                            raise ValueError("foreign record in source page")
                    account["client_events"].extend(rows["client_events"])
                    for ref, records in rows["sessions"].items():
                        account["sessions"].setdefault(ref, []).extend(records)
                    cursor = page.get("next_cursor")
                    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 200 or cursor in seen):
                        raise ValueError("invalid/repeated continuation")
                    if cursor is None:
                        if len(json.dumps(account).encode()) > 262144:
                            raise ValueError("oversized snapshot")
                        return account, {**provenance, "content_hash": hashlib.sha256(json.dumps(account, sort_keys=True).encode()).hexdigest()}
                    seen.add(cursor)
                    body.update(cursor=cursor, snapshot_id=revision)
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise DiagnosticDataGap("source_invalid") from None
        raise DiagnosticDataGap("source_truncated")
