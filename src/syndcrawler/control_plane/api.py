from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from syndcrawler.runtime.server import (
    CrawlConflictError,
    CrawlNotFoundError,
    CrawlStateError,
    ServerCrawlStatus,
    ServerRuntime,
)
from syndcrawler.runtime.server_env import env_bool, server_runtime_from_env

_bearer = HTTPBearer(auto_error=False)
_bearer_dependency = Depends(_bearer)


class CrawlSubmitRequest(BaseModel):
    urls: list[str] = Field(min_length=1)
    crawl_id: str | None = None
    follow_links: bool = True
    max_pages: int | None = Field(default=None, gt=0)


class CrawlStatusResponse(BaseModel):
    crawl_id: str
    lifecycle: str
    result_count: int
    stats: dict[str, int]
    completed: bool


class CrawlsPageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CrawlStatusResponse]


class ResultsPageResponse(BaseModel):
    crawl_id: str
    total: int
    limit: int
    offset: int
    items: list[dict[str, Any]]


class HealthResponse(BaseModel):
    ok: bool
    postgres: bool
    redis: bool


def create_app(
    runtime: ServerRuntime | None = None,
    *,
    api_token: str | None = None,
    allow_unauthenticated: bool = False,
) -> FastAPI:
    """Create the HTTP control plane.

    When `runtime` is omitted, Redis/PostgreSQL configuration is loaded during
    application lifespan from environment variables. API authentication is
    required unless `allow_unauthenticated=True` is explicitly supplied.
    """

    if not api_token and not allow_unauthenticated:
        raise RuntimeError(
            "control-plane API requires a bearer token; set SYNCRAWLER_API_TOKEN "
            "or explicitly allow unauthenticated mode"
        )

    injected_runtime = runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_runtime = injected_runtime is None
        if injected_runtime is None:
            app.state.runtime = await server_runtime_from_env()
        else:
            app.state.runtime = injected_runtime
        try:
            yield
        finally:
            if owns_runtime:
                await app.state.runtime.close()

    app = FastAPI(
        title="SyndCrawler Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    if injected_runtime is not None:
        app.state.runtime = injected_runtime

    async def require_auth(
        credentials: HTTPAuthorizationCredentials | None = _bearer_dependency,
    ) -> None:
        if allow_unauthenticated:
            return
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        assert api_token is not None
        if not hmac.compare_digest(credentials.credentials, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(CrawlNotFoundError)
    async def crawl_not_found(_request: Request, exc: CrawlNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(CrawlConflictError)
    @app.exception_handler(CrawlStateError)
    async def crawl_conflict(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def bad_request(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> dict[str, bool]:
        result = await _runtime(request).health()
        if not result["ok"]:
            raise HTTPException(status_code=503, detail=result)
        return result

    @app.post(
        "/v1/crawls",
        response_model=CrawlStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_auth)],
    )
    async def submit_crawl(payload: CrawlSubmitRequest, request: Request) -> ServerCrawlStatus:
        return await _runtime(request).submit(
            payload.urls,
            crawl_id=payload.crawl_id,
            follow_links=payload.follow_links,
            max_pages=payload.max_pages,
        )

    @app.get(
        "/v1/crawls",
        response_model=CrawlsPageResponse,
        dependencies=[Depends(require_auth)],
    )
    async def list_crawls(
        request: Request,
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> CrawlsPageResponse:
        runtime = _runtime(request)
        crawl_ids = await runtime.store.recent_crawl_ids(limit=limit, offset=offset)
        items = [await runtime.status(crawl_id) for crawl_id in crawl_ids]
        return CrawlsPageResponse(
            total=await runtime.store.manifest_count(),
            limit=limit,
            offset=offset,
            items=[
                CrawlStatusResponse.model_validate(item, from_attributes=True)
                for item in items
            ],
        )

    @app.get(
        "/v1/crawls/{crawl_id}",
        response_model=CrawlStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def crawl_status(crawl_id: str, request: Request) -> ServerCrawlStatus:
        return await _runtime(request).status(crawl_id)

    @app.get(
        "/v1/crawls/{crawl_id}/results",
        response_model=ResultsPageResponse,
        dependencies=[Depends(require_auth)],
    )
    async def crawl_results(
        crawl_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> ResultsPageResponse:
        runtime = _runtime(request)
        status_value = await runtime.status(crawl_id)
        records = await runtime.results_page(crawl_id, limit=limit, offset=offset)
        return ResultsPageResponse(
            crawl_id=crawl_id,
            total=status_value.result_count,
            limit=limit,
            offset=offset,
            items=[record.to_dict() for record in records],
        )

    @app.post(
        "/v1/crawls/{crawl_id}/pause",
        response_model=CrawlStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def pause_crawl(crawl_id: str, request: Request) -> ServerCrawlStatus:
        return await _runtime(request).pause(crawl_id)

    @app.post(
        "/v1/crawls/{crawl_id}/resume",
        response_model=CrawlStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def resume_crawl(crawl_id: str, request: Request) -> ServerCrawlStatus:
        return await _runtime(request).resume_crawl(crawl_id)

    @app.delete(
        "/v1/crawls/{crawl_id}",
        response_model=CrawlStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def cancel_crawl(crawl_id: str, request: Request) -> ServerCrawlStatus:
        return await _runtime(request).cancel(crawl_id)

    return app


def create_app_from_env() -> FastAPI:
    token = os.environ.get("SYNCRAWLER_API_TOKEN")
    allow_unauthenticated = env_bool("SYNCRAWLER_ALLOW_UNAUTHENTICATED_API", False)
    return create_app(
        None,
        api_token=token,
        allow_unauthenticated=allow_unauthenticated,
    )


def _runtime(request: Request) -> ServerRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="server runtime is not initialized")
    return runtime
