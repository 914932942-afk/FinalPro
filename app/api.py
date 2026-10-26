"""FastAPI HTTP 入口。python -m app.api 或 uvicorn app.api:app。"""
from contextlib import asynccontextmanager
import hmac
from typing import Literal, Annotated

from fastapi import FastAPI, Header, Depends, Query, BackgroundTasks, Request, Response
from fastapi.exceptions import RequestValidationError as FastAPIValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictBool, SecretStr, StringConstraints

from .cli import build_agent
from .auth import IdentityStore, Principal
from .service import TicketService
from .portal import PortalService, present_ticket
from .models import AgentState
from .errors import AgentError, IdempotencyConflict, OperationPending, AuthenticationError, AuthorizationError, ResourceNotFoundError
from .sessions import SessionStore, LoginAttemptLimiter, SESSION_COOKIE, PORTAL_HEADER, CSRF_HEADER


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")]
    password: SecretStr = Field(min_length=1, max_length=128)


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10000)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    approved: StrictBool


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    answer: str = Field(min_length=1, max_length=10000)
    round_number: int = Field(strict=True, ge=1, le=10)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=2000)
    case_revision: int | None = Field(default=None, strict=True, ge=0)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    outcome: Literal["resolved", "rejected", "archived"]
    note: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)
    case_revision: int = Field(strict=True, ge=0)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(strict=True, ge=1)
    verdict: Literal["accepted", "rejected", "inconclusive"]
    note: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    issue_resolved: StrictBool | None = None
    case_revision: int = Field(strict=True, ge=0)


class CaseRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(strict=True, ge=0, le=10000)


class FollowUpRequest(CaseRevisionRequest):
    answer: str = Field(min_length=1, max_length=10000)


class TransferRequest(CaseRevisionRequest):
    assignee: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class InformationRequest(CaseRevisionRequest):
    field: Literal["occurred_at", "symptom", "reproduction", "previous_access"]


class VerificationRequest(CaseRevisionRequest):
    verdict: Literal["confirmed", "still_failing", "recurred"]
    note: str = Field(default="", max_length=2000)


def create_app(agent=None, *, identities=None):
    @asynccontextmanager
    async def lifespan(application):
        application.state.agent = agent or build_agent()
        application.state.identities = identities or IdentityStore(application.state.agent.settings.auth_file)
        identity_path = application.state.identities.path
        application.state.sessions = SessionStore(identity_path.with_name(identity_path.stem + ".sessions.db"), application.state.identities)
        application.state.login_limiter = LoginAttemptLimiter()
        application.state.service = TicketService(application.state.agent)
        import asyncio
        await asyncio.to_thread(application.state.agent.migrate_cases)
        from .investigation_jobs import InvestigationSupervisor
        supervisor = InvestigationSupervisor(application.state.agent)
        application.state.investigation_supervisor = supervisor
        supervisor.start()
        stopping = asyncio.Event()
        async def monitor_cases():
            while not stopping.is_set():
                try:
                    await asyncio.to_thread(application.state.agent.escalate_overdue)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Case deadline check failed")
                try:
                    await asyncio.to_thread(supervisor.scan)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Investigation deadline check failed")
                try:
                    await asyncio.to_thread(application.state.agent.case_interactions.maintain)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Case reply deadline check failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=application.state.agent.settings.diagnosis_worker_poll)
                except asyncio.TimeoutError:
                    pass
        monitor = asyncio.create_task(monitor_cases())
        try:
            yield
        finally:
            stopping.set()
            await monitor
            supervisor.close()

    application = FastAPI(title="Enterprise Ops Agent", version="0.2.0", lifespan=lifespan)
    assets = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=assets), name="static")

    @application.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in {"/", "/support"} or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @application.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(assets / "index.html")

    @application.get("/support", include_in_schema=False)
    def support_page():
        return FileResponse(assets / "index.html")
    bearer = HTTPBearer(auto_error=False, description="使用本机预置用户的 API Key；非用户名或角色")

    def require_portal_request(request):
        origin = request.headers.get("origin")
        if (request.headers.get(PORTAL_HEADER) != "1" or request.headers.get("sec-fetch-site") == "cross-site"
                or (origin is not None and origin != str(request.base_url).rstrip("/"))):
            raise AuthorizationError()

    def require_csrf(request, token):
        require_portal_request(request)
        supplied = request.headers.get(CSRF_HEADER, "")
        if len(supplied) != 64 or not supplied.isascii() or not hmac.compare_digest(supplied, SessionStore.csrf_token(token)):
            raise AuthorizationError()

    def clear_cookie(response, request):
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=request.url.scheme == "https", samesite="strict")

    def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials is not None:
            return application.state.identities.authenticate(credentials.credentials)
        token = request.cookies.get(SESSION_COOKIE)
        principal = application.state.sessions.authenticate(token)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, token)
        return principal

    @application.post("/auth/login")
    def login(body: LoginRequest, request: Request, response: Response):
        require_portal_request(request)
        application.state.login_limiter.attempt(request.client.host if request.client else "local")
        principal = application.state.identities.authenticate_password(body.username, body.password.get_secret_value())
        token = application.state.sessions.create(principal)
        application.state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.set_cookie(SESSION_COOKIE, token, max_age=SessionStore.lifetime, path="/", httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
        return {"user": principal.model_dump(), "csrf_token": SessionStore.csrf_token(token)}

    @application.get("/auth/session")
    def session(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        principal = application.state.sessions.authenticate(token)
        return {"user": principal.model_dump(), "csrf_token": SessionStore.csrf_token(token)}

    @application.post("/auth/logout", status_code=204)
    def logout(request: Request):
        require_portal_request(request)
        token = request.cookies.get(SESSION_COOKIE)
        try:
            application.state.sessions.authenticate(token)
        except AuthenticationError:
            pass
        else:
            require_csrf(request, token)
            application.state.sessions.revoke(token)
        response = Response(status_code=204)
        clear_cookie(response, request)
        return response

    @application.exception_handler(FastAPIValidationError)
    async def validation_error(request, exc):
        # FastAPI's default validation response echoes input; never echo passwords.
        if request.url.path == "/auth/login":
            return JSONResponse(status_code=422, content={"error": {"code": "invalid_login", "message": "请填写有效的账号和密码。"}})
        return await request_validation_exception_handler(request, exc)

    @application.get("/portal/tickets")
    def portal_list(scope: Literal["mine", "support"] = "mine",
                    group: Literal["action_required", "processing", "completed"] | None = None,
                    query: str = Query(default="", max_length=300),
                    queue: Literal["unassigned", "mine", "overdue"] | None = None,
                    limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0),
                    principal: Principal = Depends(current_user)):
        return PortalService(application.state.service).list(principal, scope=scope, group=group, query=query, queue=queue, limit=limit, offset=offset)

    @application.get("/portal/tickets/{ticket_id}")
    def portal_detail(ticket_id: str, scope: Literal["mine", "support"] = "mine", principal: Principal = Depends(current_user)):
        view = PortalService(application.state.service).get(principal, ticket_id, scope)
        if scope == "support":
            view["support"]["assignees"] = application.state.identities.support_people(view["department"], view["requester"])
        return view

    @application.post("/portal/tickets/{ticket_id}/claim")
    def claim_case(ticket_id: str, body: CaseRevisionRequest, principal: Principal = Depends(current_user)):
        return present_ticket(application.state.service.claim(principal, ticket_id, body.revision).to_dict())

    @application.post("/portal/tickets/{ticket_id}/transfer")
    def transfer_case(ticket_id: str, body: TransferRequest, principal: Principal = Depends(current_user)):
        if principal.role not in {"manager", "admin"}:
            raise AuthorizationError()
        try:
            target = application.state.identities.principal(body.assignee)
        except AuthenticationError:
            raise ResourceNotFoundError() from None
        return present_ticket(application.state.service.transfer(principal, ticket_id, target, body.revision, body.reason).to_dict())

    @application.post("/portal/tickets/{ticket_id}/request-info")
    def request_case_info(ticket_id: str, body: InformationRequest, principal: Principal = Depends(current_user)):
        return present_ticket(application.state.service.request_info(principal, ticket_id, body.field, body.revision).to_dict())

    @application.post("/portal/tickets/{ticket_id}/follow-up")
    def follow_up(ticket_id: str, body: FollowUpRequest, background_tasks: BackgroundTasks, principal: Principal = Depends(current_user)):
        state = application.state.service.follow_up(principal, ticket_id, body.answer, body.revision)
        schedule(state, background_tasks)
        return present_ticket(state.to_dict())

    @application.post("/portal/tickets", status_code=201)
    def portal_create(body: TicketRequest, background_tasks: BackgroundTasks,
                      idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
                      principal: Principal = Depends(current_user)):
        state = application.state.service.create(principal, **body.model_dump(), idempotency_key=idempotency_key)
        schedule(state, background_tasks)
        return present_ticket(state.to_dict())

    @application.post("/portal/tickets/{ticket_id}/clarify")
    def portal_clarify(ticket_id: str, body: ClarificationRequest, background_tasks: BackgroundTasks,
                       principal: Principal = Depends(current_user)):
        state = application.state.service.clarify(principal, ticket_id, body.answer, body.round_number)
        schedule(state, background_tasks)
        return present_ticket(state.to_dict())

    @application.post("/portal/tickets/{ticket_id}/cancel")
    def portal_cancel(ticket_id: str, body: CancelRequest, principal: Principal = Depends(current_user)):
        return present_ticket(application.state.service.cancel(principal, ticket_id, body.reason, case_revision=body.case_revision).to_dict())

    @application.post("/portal/tickets/{ticket_id}/verify")
    def verify_case(ticket_id: str, body: VerificationRequest, principal: Principal = Depends(current_user)):
        return present_ticket(application.state.service.verify_case(principal, ticket_id, body.revision, body.verdict, body.note).to_dict())

    @application.exception_handler(IdempotencyConflict)
    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"error": "idempotency_conflict", "detail": str(exc)})

    @application.exception_handler(AgentError)
    async def agent_error(request, exc):
        info = exc.info().to_dict()
        headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
        response = JSONResponse(status_code=exc.http_status, content={"error": info}, headers=headers)
        if exc.http_status == 401 and request.url.path != "/auth/login":
            clear_cookie(response, request)
        if exc.http_status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @application.exception_handler(OperationPending)
    async def pending(_request, exc):
        return JSONResponse(status_code=409, headers={"Retry-After": "3"},
                            content={"error": "operation_pending", "detail": str(exc)})

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/me")
    def me(principal: Principal = Depends(current_user)):
        return principal.model_dump()

    @application.get("/runtime")
    def runtime(principal: Principal = Depends(current_user)):
        cfg = application.state.agent.settings
        primary = getattr(application.state.agent.model, "primary", application.state.agent.model)
        from .model_adapter import DeepSeekDecisionModel
        return {"model_mode": "deepseek" if isinstance(primary, DeepSeekDecisionModel) else "offline",
                "model": cfg.deepseek_model if isinstance(primary, DeepSeekDecisionModel) else "local_rule",
                "retrieval": cfg.runbook_strategy, "action_backend": cfg.action_backend,
                "simulated_business": True, "background": cfg.diagnosis_background}

    @application.post("/tickets", status_code=201)
    def create_ticket(body: TicketRequest, background_tasks: BackgroundTasks,
                      idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
                      principal: Principal = Depends(current_user)):
        state = application.state.service.create(principal, **body.model_dump(), idempotency_key=idempotency_key)
        schedule(state, background_tasks)
        return state.to_dict()

    def schedule(state, background_tasks):
        if state.ticket.investigation_status == "queued" and application.state.agent.settings.diagnosis_background:
            background_tasks.add_task(application.state.investigation_supervisor.submit, state.ticket.id, state.ticket.case_revision)

    @application.post("/portal/tickets/{ticket_id}/retry-investigation")
    def retry_investigation(ticket_id: str, body: CaseRevisionRequest, background_tasks: BackgroundTasks,
                           principal: Principal = Depends(current_user)):
        state = application.state.service.retry_investigation(principal, ticket_id, body.revision)
        schedule(state, background_tasks)
        return portal_detail(ticket_id, scope="support", principal=principal)

    @application.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, principal: Principal = Depends(current_user)):
        return application.state.service.get_ticket(principal, ticket_id)

    @application.get("/tickets")
    def list_tickets(status: str | None = Query(default=None, max_length=40),
                     limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
                     principal: Principal = Depends(current_user)):
        return application.state.service.list_tickets(principal, status, limit, offset)

    @application.get("/tickets/{ticket_id}/state")
    def get_state(ticket_id: str, principal: Principal = Depends(current_user)):
        return application.state.service.get_state(principal, ticket_id)

    @application.post("/approvals/{approval_id}")
    def approve(approval_id: str, body: ApprovalRequest, principal: Principal = Depends(current_user)):
        return application.state.service.approve(principal, approval_id, body.approved).to_dict()

    @application.post("/tickets/{ticket_id}/clarify")
    def clarify(ticket_id: str, body: ClarificationRequest, background_tasks: BackgroundTasks, principal: Principal = Depends(current_user)):
        state = application.state.service.clarify(principal, ticket_id, body.answer, body.round_number)
        schedule(state, background_tasks)
        return state.to_dict()

    @application.post("/tickets/{ticket_id}/investigate")
    def investigate(ticket_id: str, background_tasks: BackgroundTasks, principal: Principal = Depends(current_user)):
        if application.state.agent.settings.diagnosis_background:
            state = application.state.service.get_state(principal, ticket_id)
            schedule(AgentState.from_dict(state), background_tasks)
            return state
        return application.state.service.investigate(principal, ticket_id).to_dict()

    @application.post("/tickets/{ticket_id}/feedback")
    def feedback(ticket_id: str, body: FeedbackRequest, principal: Principal = Depends(current_user)):
        return application.state.service.feedback(principal, ticket_id, **body.model_dump()).to_dict()

    @application.post("/tickets/{ticket_id}/cancel")
    def cancel(ticket_id: str, body: CancelRequest, principal: Principal = Depends(current_user)):
        return application.state.service.cancel(principal, ticket_id, body.reason, case_revision=body.case_revision).to_dict()

    @application.post("/tickets/{ticket_id}/resolve")
    def resolve(ticket_id: str, body: ResolutionRequest, principal: Principal = Depends(current_user)):
        return application.state.service.resolve(principal, ticket_id, **body.model_dump()).to_dict()

    @application.get("/tickets/{ticket_id}/operations")
    def operations(ticket_id: str, principal: Principal = Depends(current_user)):
        return application.state.service.inspect_operations(principal, ticket_id)

    @application.post("/tickets/{ticket_id}/recover")
    def recover(ticket_id: str, principal: Principal = Depends(current_user)):
        return application.state.service.recover(principal, ticket_id).to_dict()

    return application


app = create_app()


def serve():
    import uvicorn
    from .config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    serve()
