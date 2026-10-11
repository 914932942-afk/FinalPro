"""FastAPI HTTP 入口。python -m app.api 或 uvicorn app.api:app。"""
from contextlib import asynccontextmanager
from typing import Literal, Annotated

from fastapi import FastAPI, Header, Depends, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .cli import build_agent
from .auth import IdentityStore, Principal
from .service import TicketService
from .errors import AgentError, IdempotencyConflict, OperationPending

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

class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    outcome: Literal["resolved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)

class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(strict=True, ge=1)
    verdict: Literal["accepted", "rejected", "inconclusive"]
    note: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    issue_resolved: StrictBool | None = None

def create_app(agent=None, *, identities=None):
    def investigate_pending(ticket_id):
        try:
            application.state.agent.investigate(ticket_id)
        except Exception as exc:

            import logging
            logging.getLogger(__name__).warning("Investigation remains recoverable: %s", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(application):
        application.state.agent = agent or build_agent()
        application.state.identities = identities or IdentityStore(application.state.agent.settings.auth_file)
        application.state.service = TicketService(application.state.agent)
        import asyncio
        async def resume_queued():
            for ticket in application.state.agent.audit.list_tickets():
                if ticket.get("investigation_status") in {"queued", "running"}:
                    await asyncio.to_thread(investigate_pending, ticket["id"])
        recovery = asyncio.create_task(resume_queued()) if application.state.agent.settings.diagnosis_background else None
        yield
        if recovery:
            await recovery

    application = FastAPI(title="Enterprise Ops Agent", version="0.2.0", lifespan=lifespan)
    assets = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=assets), name="static")

    @application.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @application.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(assets / "index.html")
    bearer = HTTPBearer(auto_error=False, description="使用本机预置用户的 API Key；非用户名或角色")

    def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        return application.state.identities.authenticate(credentials.credentials if credentials else None)

    @application.exception_handler(IdempotencyConflict)
    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"error": "idempotency_conflict", "detail": str(exc)})

    @application.exception_handler(AgentError)
    async def agent_error(_request, exc):
        info = exc.info().to_dict()
        headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
        return JSONResponse(status_code=exc.http_status, content={"error": info}, headers=headers)

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
            background_tasks.add_task(investigate_pending, state.ticket.id)

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
    def investigate(ticket_id: str, principal: Principal = Depends(current_user)):
        return application.state.service.investigate(principal, ticket_id).to_dict()

    @application.post("/tickets/{ticket_id}/feedback")
    def feedback(ticket_id: str, body: FeedbackRequest, principal: Principal = Depends(current_user)):
        return application.state.service.feedback(principal, ticket_id, **body.model_dump()).to_dict()

    @application.post("/tickets/{ticket_id}/cancel")
    def cancel(ticket_id: str, body: CancelRequest, principal: Principal = Depends(current_user)):
        return application.state.service.cancel(principal, ticket_id, body.reason).to_dict()

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
