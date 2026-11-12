"""The website's API: device search, device pages and the chatbot, over HTTP.

  GET  /api/health                       service, dataset and fingerprints (no BigQuery)
  GET  /api/search?q=<text>&limit=10     search box hits (local index, no BigQuery)
  GET  /api/entity/<kind>/<entity_id>    a device page: device_summary() of one entity
  POST /api/answer                       {"question": "...", "entity": null | {...}} -> an answer

Contract: the front-end handover (draft API, section 2). Responses never carry SQL or query
parameters: "sources" are trimmed to the query name and tables. Errors are JSON with a status
(budget, error, bad request), never a traceback. Questions are not logged.

When VIGIL_WEB_DIST names the front end's build folder (dist/), the site is served from "/"
too, so one container serves both. VIGIL_CORS_ORIGINS (comma-separated) allows a dev server
such as http://localhost:5173.

Run (from Model-Development/chatbot/):
  VIGIL_BQ_DATASET=vigil_md_dev uvicorn api.app:app --port 8000
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from answer import pipeline
from answer import templates
from query_layer import backend as ql_backend
from query_layer import config
from query_layer import summary
from query_router import guardrails

MAX_QUESTION_CHARS = 1000
MAX_QUERY_CHARS = 200
HIT_FIELDS = ("id", "kind", "entity_id", "title", "subtitle", "reports", "deaths", "injuries",
              "product_codes")
# Per client: questions and device pages read BigQuery, so they are limited; search is local.
RATE_LIMIT = int(os.environ.get("VIGIL_RATE_LIMIT_PER_MINUTE", "30"))


def public(obj):
    """Removes SQL and parameters: every "sources" list becomes [{query, tables}]."""
    if isinstance(obj, dict):
        return {k: ([{"query": s.get("query"), "tables": list(s.get("tables") or [])}
                     for s in v] if k == "sources" and isinstance(v, list) else public(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [public(x) for x in obj]
    return obj


class EntityKey(BaseModel):
    entity_type: str
    entity_id: str = Field(min_length=1, max_length=200)


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    entity: EntityKey | None = None


class RateLimiter:
    """At most `per_minute` calls per client in any 60-second window (in memory, per process)."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self.calls: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.calls[client]
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def create_app(bot: pipeline.Chatbot | None = None, dataset: str | None = None,
               web_dist: str | None = None, rate_limit: int = RATE_LIMIT) -> FastAPI:
    """Builds the app. Tests pass a Chatbot with fakes; the server opens the real one."""
    dataset = dataset or config.DATASET
    state = {"bot": bot}
    limiter = RateLimiter(rate_limit)
    app = FastAPI(title="Vigil MD API", docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")

    origins = [o.strip() for o in os.environ.get("VIGIL_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        from fastapi.middleware.cors import CORSMiddleware  # pylint: disable=import-outside-toplevel
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                           allow_headers=["Content-Type"])

    def chatbot() -> pipeline.Chatbot:
        if state["bot"] is None:
            state["bot"] = pipeline.Chatbot.open(dataset)
        return state["bot"]

    def limited(request: Request) -> None:
        # Behind Cloud Run's proxy this is the visitor's address only because uvicorn trusts
        # the proxy's X-Forwarded-For (--forwarded-allow-ips in the Dockerfile). A visitor can
        # send a false header to get a fresh limit; the per-question cost cap and the daily
        # BigQuery quota still bound the cost.
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail={
                "status": "rate_limited", "text": "Too many questions in a minute. Please wait "
                                                  "a moment and try again."})

    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) else {"status": "bad_request",
                                                                "text": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/api/health")
    def health():
        bot_ = chatbot()
        return {"status": "ok", "dataset": dataset,
                "rules_fingerprint": guardrails.rules_fingerprint(),
                "template_fingerprint": templates.templates_fingerprint(),
                "index_built": getattr(bot_.index, "meta", {}).get("built_at")}

    @app.get("/api/search")
    def search(q: str = Query(min_length=2, max_length=MAX_QUERY_CHARS),
               limit: int = Query(10, ge=1, le=25)):
        hits = chatbot().index.search(q, limit=limit)
        return {"query": q, "hits": [{k: getattr(h, k) for k in HIT_FIELDS} for h in hits]}

    @app.get("/api/entity/{kind}/{entity_id:path}")
    def entity(kind: str, entity_id: str, request: Request):
        if kind not in config.ENTITY_TYPES:
            raise HTTPException(status_code=404, detail={
                "status": "not_found", "text": f"Unknown kind {kind!r}."})
        limited(request)
        bot_ = chatbot()
        budget = (ql_backend.Budget() if bot_.max_gb is None
                  else ql_backend.Budget(max_gb=bot_.max_gb))
        try:
            page = summary.device_summary(bot_.backend, kind, entity_id, budget)
        except ql_backend.QueryBudgetExceeded:
            raise HTTPException(status_code=413, detail={"status": "budget",
                                                         "text": templates.BUDGET}) from None
        except pipeline.DATA_ERRORS:
            logging.exception("device page failed")
            raise HTTPException(status_code=503, detail={"status": "error",
                                                         "text": templates.SERVICE_DOWN}) from None
        return public(page)

    @app.post("/api/answer")
    def answer(body: AnswerRequest, request: Request):
        limited(request)
        entity = None
        if body.entity:
            if body.entity.entity_type not in config.ENTITY_TYPES:
                raise HTTPException(status_code=422, detail={
                    "status": "bad_request", "text": "Unknown entity_type."})
            entity = (body.entity.entity_type, body.entity.entity_id)
        out = chatbot().answer(body.question, entity=entity)
        out.pop("question", None)            # the client sent it; nothing personal echoed back
        return public(out)

    dist = web_dist or os.environ.get("VIGIL_WEB_DIST")
    if dist and Path(dist).is_dir():
        add_site(app, Path(dist))
    return app


def add_site(app: FastAPI, dist: Path) -> None:
    """Serves the front end's build from "/". The front end is a Next.js static export: one
    HTML file per page, so /device (opened, reloaded or shared, with ?kind=…&id=…) gets
    device.html. A file in dist/ is served as itself; any other path gets 404.html with a 404
    when the build has one, else index.html (a single-page build without page files).
    Unknown /api/... paths stay JSON 404s, and nothing outside dist/ can be read."""
    from fastapi.responses import FileResponse  # pylint: disable=import-outside-toplevel
    root = dist.resolve()
    index, not_found = root / "index.html", root / "404.html"

    @app.get("/{path:path}", include_in_schema=False)
    def site(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail={"status": "not_found",
                                                         "text": "Not found."})
        if not path:
            return FileResponse(index)
        target = (root / path).resolve()
        if target.is_file() and target.is_relative_to(root):
            return FileResponse(target)
        page = (root / f"{path.rstrip('/')}.html").resolve()   # /device -> device.html
        if page.is_file() and page.is_relative_to(root):
            return FileResponse(page)
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        return FileResponse(index)


app = create_app()
