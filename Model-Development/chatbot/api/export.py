"""The recall finder and the exports of search results to PDF and Excel (export/ does the work).

  GET /api/recalls?<filters>&page=1                    recall finder page: 50 rows + the total
  GET /api/export/recalls?format=pdf|xlsx&<filters>    the recall finder's file
  GET /api/export/entity/<kind>/<id>?format=json|pdf|xlsx&<filters>
                                                       a device page with filters: JSON for the
                                                       page, or its file (same data function)
  GET /api/export/search?q=<text>&format=pdf|xlsx      the search results list's file
  GET /api/export/records?q=<text>&format=pdf|xlsx     all records of the results shown on the
                                                       home screen: recalls, enforcement, MAUDE

The recall finder and the search list read the local search index (no BigQuery); a device page
reads the summary tables (the same few MB as /api/entity, with its cost cap). Filters are
checked against allow-lists (422 otherwise). Over a format's row limit: 413 with the count, never
a silently cut file. Files: at most EXPORT_RATE_LIMIT a minute per visitor. The visitor's text
filters are never logged, and file names are built from fixed parts only.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from answer import pipeline
from answer import templates
from export import data, document, filters, pdf, xlsx
from query_layer import backend as ql_backend
from query_layer import config

EXPORT_RATE_LIMIT = int(os.environ.get("VIGIL_EXPORT_RATE_LIMIT_PER_MINUTE", "5"))
PAGE_RATE_LIMIT = int(os.environ.get("VIGIL_RATE_LIMIT_PER_MINUTE", "30"))   # as /api/entity
# "All records" files read BigQuery (about 20-80 MB each on PROD): stricter than other files.
RECORDS_RATE_LIMIT = int(os.environ.get("VIGIL_RECORDS_RATE_LIMIT_PER_MINUTE", "2"))
MAX_QUERY_CHARS = 200
FORMATS = {"pdf": (pdf, data.PDF_MAX), "xlsx": (xlsx, data.XLSX_MAX)}
FILE_SLUG = {"device": "device", "manufacturer": "manufacturer", "product_code": "product-code"}


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _bad(text: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"status": "bad_request", "text": text})


def _too_many(e: data.TooManyRows, what: str = "recalls") -> HTTPException:
    return HTTPException(status_code=413, detail={
        "status": "too_many", "count": e.count, "limit": e.limit,
        "text": f"{e.count:,} {what} match. Narrow the filters: this file holds at most "
                f"{e.limit:,}."})


def _filters(request: Request, scope: str) -> filters.Filters:
    try:
        return filters.parse(list(request.query_params.multi_items()), scope)
    except filters.FilterError as e:
        raise _bad(str(e)) from None


def _format(fmt: str, allowed=FORMATS) -> str:
    if fmt not in allowed:
        raise _bad(f"format must be one of {', '.join(allowed)}.")
    return fmt


def _file(doc: document.Document, fmt: str, slug: str) -> Response:
    module = FORMATS[fmt][0]
    body = module.build(doc)
    name = f"vigil-md-{slug}-{doc.made_at:%Y-%m-%d}.{fmt}"        # fixed parts only
    return Response(content=body, media_type=module.MEDIA_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def make_router(get_bot: Callable[[], pipeline.Chatbot], rate_limiter) -> APIRouter:
    """The routes; get_bot gives the chatbot (its search index and BigQuery backend),
    rate_limiter is api.app.RateLimiter (passed in to avoid a circular import)."""
    router = APIRouter()
    file_limiter = rate_limiter(EXPORT_RATE_LIMIT)
    page_limiter = rate_limiter(PAGE_RATE_LIMIT)
    records_limiter = rate_limiter(RECORDS_RATE_LIMIT)

    def limit_files(request: Request) -> None:
        if not file_limiter.allow(_client(request)):
            raise HTTPException(status_code=429, detail={
                "status": "rate_limited", "text": "Too many downloads in a minute. Please "
                                                  "wait a moment and try again."})

    def limit_pages(request: Request) -> None:
        if not page_limiter.allow(_client(request)):
            raise HTTPException(status_code=429, detail={
                "status": "rate_limited", "text": "Too many requests in a minute. Please wait "
                                                  "a moment and try again."})

    def limit_records(request: Request) -> None:
        if not records_limiter.allow(_client(request)):
            raise HTTPException(status_code=429, detail={
                "status": "rate_limited", "text": "Too many record downloads in a minute. Please "
                                                  "wait a moment and try again."})

    def records_or_error(bot, q: str) -> dict:
        try:
            return data.records_view(bot, q, ql_backend.Budget(max_gb=data.RECORDS_MAX_GB))
        except ql_backend.QueryBudgetExceeded:
            raise HTTPException(status_code=413, detail={
                "status": "budget", "text": "These results have too many records for one file. "
                                            "Open a result's page and download it there."}) from None
        except pipeline.DATA_ERRORS:
            logging.exception("records failed")             # no search text in the log
            raise HTTPException(status_code=503, detail={"status": "error",
                                                         "text": templates.SERVICE_DOWN}) from None

    def index():
        return get_bot().index

    @router.get("/api/recalls")
    def recalls(request: Request, page: int = Query(1, ge=1, le=100_000)):
        f = _filters(request, "recalls")
        idx = index()
        result = data.recall_page(idx.con, f, page)
        return {**result, "filters": filters.describe(f),
                "data_as_of": document.as_of(idx.meta.get("built_at"))}

    @router.get("/api/export/recalls")
    def export_recalls(request: Request, format: str = Query(...)):  # pylint: disable=redefined-builtin
        fmt = _format(format)
        f = _filters(request, "recalls")
        limit_files(request)
        idx = index()
        try:
            rows = data.recalls_for_file(idx.con, f, FORMATS[fmt][1])
        except data.TooManyRows as e:
            raise _too_many(e) from None
        doc = document.recalls_document(rows, filters.describe(f), idx.meta.get("built_at"),
                                        dt.datetime.now(dt.timezone.utc))
        return _file(doc, fmt, "recalls")

    @router.get("/api/export/entity/{kind}/{entity_id:path}")
    def export_entity(kind: str, entity_id: str, request: Request,
                      format: str = Query(...)):  # pylint: disable=redefined-builtin
        if kind not in config.ENTITY_TYPES:
            raise HTTPException(status_code=404, detail={"status": "not_found",
                                                         "text": "Unknown kind."})
        fmt = _format(format, {"json": None, **FORMATS})
        f = _filters(request, "entity")
        (limit_pages if fmt == "json" else limit_files)(request)
        bot = get_bot()
        budget = (ql_backend.Budget() if bot.max_gb is None
                  else ql_backend.Budget(max_gb=bot.max_gb))
        try:
            view = data.entity_view(bot.backend, kind, entity_id, f, budget,
                                    limit=data.XLSX_MAX if fmt == "json" else FORMATS[fmt][1])
        except data.TooManyRows as e:
            raise _too_many(e) from None
        except ql_backend.QueryBudgetExceeded:
            raise HTTPException(status_code=413, detail={"status": "budget",
                                                         "text": templates.BUDGET}) from None
        except pipeline.DATA_ERRORS:
            logging.exception("device export failed")       # no filter values in the log
            raise HTTPException(status_code=503, detail={"status": "error",
                                                         "text": templates.SERVICE_DOWN}) from None
        if fmt == "json":
            page = {k: v for k, v in view.items() if k != "all_recalls"}
            page["filters"] = filters.describe(f)
            page["source_summary"] = data.page_source_summary(
                view, bot.index, kind, entity_id, [r["recall_number"] for r in view["all_recalls"]],
                filtered=not f.empty)
            page["sources"] = [{"query": s.get("query"), "tables": list(s.get("tables") or [])}
                               for s in page.get("sources", [])]       # no SQL leaves the API
            return page
        doc = document.entity_document(view, filters.describe(f), dt.datetime.now(dt.timezone.utc))
        return _file(doc, fmt, FILE_SLUG[kind])

    @router.get("/api/export/search")
    def export_search(request: Request, q: str = Query(min_length=2, max_length=MAX_QUERY_CHARS),
                      format: str = Query(...)):  # pylint: disable=redefined-builtin
        fmt = _format(format)
        limit_files(request)
        idx = index()
        hits = data.search_hits(idx, q)
        doc = document.search_document(hits, q, idx.meta.get("built_at"),
                                       dt.datetime.now(dt.timezone.utc))
        return _file(doc, fmt, "search")

    @router.get("/api/export/records")
    def export_records(request: Request, q: str = Query(min_length=2, max_length=MAX_QUERY_CHARS),
                       format: str = Query(...)):  # pylint: disable=redefined-builtin
        fmt = _format(format)
        limit_files(request)
        limit_records(request)
        view = records_or_error(get_bot(), q)
        doc = document.records_document(view, q, data.PER_RESULT, dt.datetime.now(dt.timezone.utc))
        return _file(doc, fmt, "records")

    return router
