"""Web process: health/readiness, OAuth, admin dashboard, approval queue and JSON API."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import approvals
from .app_context import AppContext, build_context, ensure_source
from .errors import AgentError, ErrorCategory, record_error
from .logging_setup import correlation_id_var, get_logger, log, new_id
from .metrics import dashboard_data
from .policy import ACTION_CATALOG, ProposedAction
from .qualification import Candidate, qualify

SESSION_COOKIE = "rs_session"
SESSION_MAX_AGE = 8 * 3600
_logger = get_logger("rs_agent.web")

_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(default=True, default_for_string=True),
)

LOGIN_HTML = _env.get_template("login.html")

DASHBOARD_HTML = _env.get_template("dashboard.html")


def create_app(ctx: AppContext | None = None) -> Starlette:
    ctx = ctx or build_context()
    signer = URLSafeTimedSerializer(ctx.settings.session_secret or "dev-only-insecure-secret-change-me", salt="rs-session")

    def _session(request: Request) -> dict[str, Any] | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data: dict[str, Any] = signer.loads(raw, max_age=SESSION_MAX_AGE)
            return data
        except BadSignature:
            return None

    def _token_ok(token: str | None) -> bool:
        expected = ctx.settings.admin_token
        if not expected or not token:
            return False
        return hmac.compare_digest(token.encode(), expected.encode())

    def _api_authed(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and _token_ok(auth[7:].strip()):
            return True
        return _session(request) is not None

    def _err(exc: AgentError) -> JSONResponse:
        status = {ErrorCategory.VALIDATION: 400, ErrorCategory.AUTHENTICATION: 401, ErrorCategory.PERMISSION: 403,
                  ErrorCategory.POLICY: 409, ErrorCategory.RATE_LIMIT: 429}.get(exc.category, 502)
        return JSONResponse({"error": str(exc), "category": exc.category.value}, status_code=status)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(request: Request) -> Response:
        if not ctx.db.ping():
            return JSONResponse({"ready": False, "database": "FAILED"}, status_code=503)
        problems = ctx.startup_problems
        return JSONResponse({"ready": True, "config_warnings": len(problems)}, status_code=200)

    async def login_form(request: Request) -> Response:
        return HTMLResponse(LOGIN_HTML.render(error=None))

    async def login(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("token") or "")
        if not _token_ok(token):
            log(_logger, logging.WARNING, "failed admin login", ip=request.client.host if request.client else None)
            return HTMLResponse(LOGIN_HTML.render(error="Invalid token"), status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, signer.dumps({"csrf": new_id()}), max_age=SESSION_MAX_AGE, httponly=True,
                        secure=ctx.settings.is_production, samesite="strict")
        return resp

    async def dashboard(request: Request) -> Response:
        session = _session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        data = dashboard_data(ctx.db, ctx.linkedin, ctx.claude)
        if "qualification" not in data:
            return HTMLResponse("<h1>Database unavailable</h1>", status_code=503)
        queue = approvals.list_queue(ctx.db, "pending")
        manual = [q for q in approvals.list_queue(ctx.db, "approved") if q["state"] == "MANUAL_ACTION_REQUIRED"]
        return HTMLResponse(DASHBOARD_HTML.render(d=_Obj(data), queue=queue, approved_manual=manual, csrf=session["csrf"]))

    async def queue_form(request: Request) -> Response:
        session = _session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf") or ""), str(session.get("csrf"))):
            return JSONResponse({"error": "invalid CSRF token"}, status_code=403)
        decision = str(form.get("decision") or "")
        content = str(form.get("content") or "") or None
        try:
            if decision == "approve" and content:
                item = ctx.db.fetchone("SELECT proposed_content, edited_content FROM action_queue WHERE id = ?",
                                       (request.path_params["queue_id"],))
                current = (item or {}).get("edited_content") or (item or {}).get("proposed_content")
                if item and content.strip() != (current or "").strip():
                    approvals.decide(ctx.db, ctx.settings, request.path_params["queue_id"], "edit", "admin", content)
            approvals.decide(ctx.db, ctx.settings, request.path_params["queue_id"], decision, "admin",
                             content if decision == "edit" else None)
        except AgentError as exc:
            return _err(exc)
        return RedirectResponse("/", status_code=303)

    async def api_dashboard(request: Request) -> Response:
        if not _api_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(dashboard_data(ctx.db, ctx.linkedin, ctx.claude))

    async def api_queue(request: Request) -> Response:
        if not _api_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"items": approvals.list_queue(ctx.db, request.query_params.get("status", "pending"))})

    async def api_decide(request: Request) -> Response:
        if not _api_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _json(request)
        try:
            item = approvals.decide(ctx.db, ctx.settings, request.path_params["queue_id"], str(body.get("decision", "")),
                                    "api", body.get("content"), body.get("note"))
        except AgentError as exc:
            return _err(exc)
        return JSONResponse({"status": item["status"]})

    async def api_candidates(request: Request) -> Response:
        """Ingest candidates from an authorized source (never from LinkedIn scraping)."""
        if not _api_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _json(request)
        src = body.get("source") or {}
        try:
            source_id = ensure_source(ctx.db, str(src.get("kind", "")), str(src.get("name", "")), src.get("url"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        items = body.get("candidates")
        if not isinstance(items, list) or not items or len(items) > 200:
            return JSONResponse({"error": "candidates must be a list of 1–200 items"}, status_code=400)
        out = []
        for raw in items:
            if not isinstance(raw, dict):
                return JSONResponse({"error": "each candidate must be an object"}, status_code=400)
            cand = Candidate(
                full_name=str(raw.get("full_name", ""))[:120], source_id=source_id,
                source_url=_str(raw.get("source_url")), role_title=_str(raw.get("role_title")),
                company_name=_str(raw.get("company_name")), industry=_str(raw.get("industry")),
                region=_str(raw.get("region")), headline=_str(raw.get("headline")),
                profile_url=_str(raw.get("profile_url")), evidence_date=_str(raw.get("evidence_date")),
            )
            q = qualify(ctx.db, cand)
            out.append({"profile_id": q.profile_id, "qualified": q.qualified, "score": q.score,
                        "rejection_reason": q.rejection_reason})
        return JSONResponse({"results": out})

    async def api_propose(request: Request) -> Response:
        if not _api_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _json(request)
        action_type = str(body.get("action_type", ""))
        if action_type not in ACTION_CATALOG:
            return JSONResponse({"error": "unknown action_type"}, status_code=400)
        result = approvals.propose(ctx.db, ctx.settings, ProposedAction(
            action_type=action_type, target_kind=_str(body.get("target_kind")), target_id=_str(body.get("target_id")),
            target_label=_str(body.get("target_label")), content=_str(body.get("content")),
            source_id=_str(body.get("source_id")), reason=_str(body.get("reason"))), actor="api")
        return JSONResponse({"action_id": result.action_id, "queue_id": result.queue_id,
                             "state": result.decision.state.value, "reasons": result.decision.reasons})

    async def oauth_start(request: Request) -> Response:
        if _session(request) is None:
            return RedirectResponse("/login", status_code=303)
        try:
            return RedirectResponse(ctx.linkedin.authorization_url(), status_code=303)
        except AgentError as exc:
            return _err(exc)

    async def oauth_callback(request: Request) -> Response:
        if request.query_params.get("error"):
            record_error(ctx.db, ErrorCategory.AUTHENTICATION, "oauth", "user denied or LinkedIn returned an error")
            return HTMLResponse("<p>LinkedIn authorization was not completed.</p>", status_code=400)
        try:
            bundle = ctx.linkedin.exchange_code(request.query_params.get("code", ""), request.query_params.get("state", ""))
        except AgentError as exc:
            record_error(ctx.db, exc.category, "oauth", str(exc))
            return HTMLResponse("<p>LinkedIn authorization failed. Check the dashboard errors.</p>", status_code=400)
        return HTMLResponse(f"<p>LinkedIn connected. Scopes: {', '.join(bundle.scopes) or 'none'}.</p><a href='/'>Dashboard</a>")

    async def logout(request: Request) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    routes = [
        Route("/health", health), Route("/ready", ready),
        Route("/login", login_form, methods=["GET"]), Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST", "GET"]),
        Route("/", dashboard), Route("/queue/{queue_id}", queue_form, methods=["POST"]),
        Route("/oauth/linkedin/start", oauth_start), Route("/oauth/linkedin/callback", oauth_callback),
        Route("/api/dashboard", api_dashboard), Route("/api/queue", api_queue),
        Route("/api/queue/{queue_id}", api_decide, methods=["POST"]),
        Route("/api/candidates", api_candidates, methods=["POST"]),
        Route("/api/actions", api_propose, methods=["POST"]),
    ]
    app_inner = Starlette(routes=routes, middleware=[Middleware(SecurityHeadersMiddleware)])

    app_inner.state.ctx = ctx
    return app_inner


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: correlation id + security headers on every response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-correlation-id", b"").decode("latin-1")
        cid = (incoming or new_id())[:64]
        token = correlation_id_var.set(cid)

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-correlation-id"] = cid
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["x-frame-options"] = "DENY"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            correlation_id_var.reset(token)


class _Obj(dict[str, Any]):
    """Dict with attribute access for templates."""

    def __getattr__(self, item: str) -> Any:
        value = self.get(item)
        return _Obj(value) if isinstance(value, dict) else value


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:500] if s else None


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
