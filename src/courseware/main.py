import logging
from importlib.metadata import version
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from . import admin, auth, catalog
from .config import settings
from .db import engine
from .errors import Problem

logger = logging.getLogger("courseware")
app = FastAPI(title="Courseware API", version=version("courseware-api"), docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(admin.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def error_response(request, status, code, message, details=None):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return error_response(request, exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return error_response(
        request,
        422,
        "INPUT_INVALID",
        "输入内容不符合要求",
        [{"path": ".".join(map(str, error["loc"])), "reason": error["type"]} for error in exc.errors()],
    )


@app.exception_handler(IntegrityError)
async def integrity_handler(request, exc):
    return error_response(request, 409, "DATA_CONFLICT", "数据已存在或被其他操作修改，请刷新后重试")


@app.exception_handler(HTTPException)
async def http_handler(request, exc):
    return error_response(request, exc.status_code, "HTTP_ERROR", "请求的资源或方法不可用")


@app.exception_handler(Exception)
async def unexpected_handler(request, exc):
    # Never log request bodies, SQL parameters, cookies, credentials or raw Git output.
    logger.error(
        "request_failed request_id=%s type=%s", getattr(request.state, "request_id", ""), type(exc).__name__
    )
    return error_response(request, 500, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试")


@app.get("/health/live")
def live():
    return {"status": "ok", "version": app.version}


@app.get("/health/ready")
def ready():
    with engine().connect() as connection:
        connection.execute(text("SELECT 1 FROM alembic_version"))
    if not (settings().storage_path / "releases").is_dir():
        raise Problem(503, "STORAGE_UNAVAILABLE", "发布目录不可用")
    return {"status": "ready"}
