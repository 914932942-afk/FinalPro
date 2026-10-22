"""Generate a stratified investigation benchmark with novel telemetry variants.

The fixture is synthetic and explicitly labelled.  It is used to compare the
maintained regex baseline with the real diagnostic LLM under the same tools and
budget; labels are never passed to the model.
"""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "it_investigation_large_20261011.json"


TEMPLATES = {
    "kb-v01": {
        "resource": "vpn",
        "client": [
            "VPN tunnel is up but the corporate portal cannot be resolved.",
            "The remote session connects; the internal hostname is not found.",
            "Name resolution fails only for the private portal after VPN login.",
            "The endpoint reaches the network but the portal hostname has no DNS answer.",
            "VPN access works by address, while the service name returns no host.",
        ],
        "session": [
            "resolver returned NXDOMAIN for portal.corp.test while 10.21.1.8 responded to ping.",
            "DNS lookup for portal.corp.test returned no such name; direct IP access succeeded.",
            "the private resolver had no record for portal.corp.test; the tunnel and target IP were reachable.",
            "name resolution failed for portal.corp.test, although the same session reached 10.21.1.8.",
            "the hostname lookup was refused by the resolver and the direct address remained reachable.",
        ],
    },
    "kb-v02": {
        "resource": "vpn",
        "client": [
            "VPN connects but the intranet subnet is unreachable.",
            "The tunnel is established; traffic to the internal network follows the home gateway.",
            "Remote login succeeds but the pushed private route is absent.",
            "The endpoint cannot reach the corporate subnet after connection.",
            "The tunnel comes up and packets for the internal service leave on the local route.",
        ],
        "session": [
            "the server pushed 10.21.0.0/16 but the client did not install that route; packets used the home gateway.",
            "route installation was skipped for the pushed corporate subnet and the destination matched the default route.",
            "the private network overlapped with a local route, so the VPN route was not selected.",
            "the route table contains no active entry for 10.21.0.0/16 after the tunnel handshake.",
            "the client retained the local gateway for the corporate destination instead of the VPN interface.",
        ],
    },
    "kb-v03": {
        "resource": "vpn",
        "client": [
            "TLS negotiation stops after the tunnel starts.",
            "The VPN client rejects the server certificate.",
            "The connection reaches certificate validation and then fails.",
            "The remote endpoint is reachable but the trust check does not complete.",
            "The secure channel cannot be established after the initial handshake.",
        ],
        "session": [
            "certificate notAfter=2026-09-20 is earlier than the connection time 2026-09-29.",
            "the presented certificate validity ended on 2026-09-20; system time is 2026-09-29.",
            "verification reports that the server certificate is past its expiration date.",
            "the certificate validity interval does not include the current session timestamp.",
            "TLS verification failed because the certificate had already expired before this connection.",
        ],
    },
    "kb-e01": {
        "resource": "erp",
        "client": [
            "ERP report pages keep loading after login.",
            "The ERP request is accepted but waits before data is returned.",
            "The reports screen spins while the session remains authenticated.",
            "ERP requests intermittently stall during database access.",
            "The application is responsive at login but report retrieval blocks.",
        ],
        "session": [
            "connection pool acquisition exceeded 30 seconds while the health probe completed quickly.",
            "the database pool was exhausted and a request waited for a connection until timeout.",
            "pool checkout timed out before SQL execution; the lightweight health query was normal.",
            "application workers waited for an available database connection and the acquire deadline elapsed.",
            "the request timed out in connection acquisition rather than in the business query.",
        ],
    },
    "kb-e02": {
        "resource": "erp",
        "client": [
            "ERP sends the user back to the login page after authentication.",
            "The sign-in succeeds but the application rejects the returned token.",
            "ERP authentication completes and then the API responds unauthorized.",
            "The account is accepted by the identity provider but ERP will not open.",
            "The session is created and immediately rejected by the ERP backend.",
        ],
        "session": [
            "token audience is inventory-api while the ERP endpoint expects erp-api.",
            "signature verification passed; the aud claim does not match the configured erp-api audience.",
            "the access token was issued for inventory-api and was rejected by the ERP resource server.",
            "the audience claim differs from the ERP API audience even though the signature is valid.",
            "ERP denied the token because its audience was another service rather than erp-api.",
        ],
    },
    "kb-e03": {
        "resource": "erp",
        "client": [
            "The ERP transaction fails while saving a change.",
            "A business request does not finish and the page reports an error.",
            "The update request is rolled back by the database.",
            "ERP writes intermittently fail under concurrent activity.",
            "The transaction remains pending and then returns an execution error.",
        ],
        "session": [
            "two transactions wait on each other's locks; the database detected a deadlock and rolled one back.",
            "the lock manager found a circular wait between tx17 and tx19 and aborted a victim.",
            "a deadlock was detected during the update and one transaction was selected for rollback.",
            "transactions formed a lock cycle; the database terminated one request to break the cycle.",
            "the transaction log records a circular lock dependency followed by victim rollback.",
        ],
    },
    "unknown-vpn": {
        "resource": "vpn",
        "client": [
            "VPN failed during a remote work session; the reason is not in the client record.",
            "The tunnel error is reported without a diagnostic code.",
            "The remote connection is unstable but no component is identified.",
            "VPN access is unavailable and the available client telemetry is incomplete.",
            "The endpoint reports a failure with no supporting network measurements.",
        ],
        "session": [
            "gateway returned an error; packet trace, route table and resolver measurements were not captured.",
            "the session ended with a generic timeout and no downstream event was recorded.",
            "the server log contains only an error marker; the cause and affected subsystem are unknown.",
            "the tunnel status changed to failed but there is no evidence for DNS, routing or certificate failure.",
            "the current session has insufficient telemetry to associate the error with a maintained procedure.",
        ],
    },
    "unknown-erp": {
        "resource": "erp",
        "client": [
            "ERP returned an error while loading a page; no category is known.",
            "The application failed during a request with incomplete telemetry.",
            "ERP is unavailable for this user and the client record has no diagnostic detail.",
            "A page returned an error but the failing downstream service is not identified.",
            "The current request failed without a trace identifier or component name.",
        ],
        "session": [
            "gateway returned 504; upstream trace, pool metrics and database events are unavailable.",
            "the request timed out and no service-specific evidence was captured.",
            "only a generic server error is present; there is not enough current evidence for a runbook.",
            "the backend returned an error without identifying authentication, pool or transaction details.",
            "the session ended before downstream telemetry was recorded, so the cause is inconclusive.",
        ],
    },
}


def build():
    cases = []
    for label, spec in TEMPLATES.items():
        expected = None if label.startswith("unknown-") else label
        for index in range(15):
            resource = spec["resource"]
            case_id = f"large-{label}-{index+1:02d}"
            timestamp = f"2026-10-{(index % 9) + 1:02d} 10:{30 + index:02d}"
            session_id = f"session-large-{label}-{index+1:02d}"
            log_id = f"log-large-{label}-{index+1:02d}"
            client_id = f"client-large-{label}-{index+1:02d}"
            client_text = f"{timestamp} {spec['client'][index % len(spec['client'])]} session_ref={session_id}; trace_id=trace-{index+1:03d}"
            session_text = f"{timestamp} {spec['session'][index % len(spec['session'])]} trace_id=trace-{index+1:03d}"
            # A benign heartbeat makes the model associate evidence rather than match one isolated keyword.
            heartbeat = {"id": f"heartbeat-{index+1:03d}", "text": f"{timestamp} client heartbeat observed; no action requested."}
            cases.append({
                "id": case_id,
                "split": "large-test",
                "resource": resource,
                "request": f"请排查当前 {resource.upper()} 会话在 {timestamp[-5:]} 的故障，并只根据当前会话给出可引用的判断。",
                "telemetry": {"simulated": True, "resources": {resource: {"accounts": {"alice": {
                    "client_events": [{"id": client_id, "text": client_text, "session_ref": session_id}, heartbeat],
                    "sessions": {session_id: [{"id": log_id, "text": session_text}]},
                }}}}},
                "expected_runbook": expected,
                "required_log_ids": [log_id],
                "family": label,
            })
    return {"description": "120-case stratified synthetic investigation benchmark; 15 independently rendered telemetry cases per family; labels never sent to the model.", "cases": cases}


if __name__ == "__main__":
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} cases={len(payload['cases'])}")
