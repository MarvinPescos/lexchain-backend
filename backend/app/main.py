import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langfuse import get_client
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.all_models  # Ensure all SQLAlchemy models are registered at startup
from app.features.admin import router as admin_router
from app.features.auth import router as auth_router

# from app.features.blockchain import router as blockchain_router
from app.features.books import router as book_router
from app.features.documents import router as document_router
from app.features.google import router as google_router
from app.features.notifications import router as notification_router
from app.features.retrieval import router as retrieval_router
from app.features.users import router as user_router
from app.core import settings
from app.infrastructure.database import async_engine, check_schema_revision, close_db
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import HTTP_ERROR_TYPES, BaseAppException
from app.shared.utils.to_dict import build_error_response


# Added 2026-09-12 22:57 PST — dispose the connection pool on shutdown.
# close_db() has existed since the database package was written and was never
# called: the process just exited and left the OS to drop the sockets, so
# Postgres kept the sessions until it noticed. lifespan, not the older
# @app.on_event("shutdown") — FastAPI deprecated on_event in favour of this.
# Startup checks the schema revision first — see database/schema.py (issue #25).
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_engine.connect() as connection:
        await check_schema_revision(connection)
    yield
    await close_db()


# Swagger section order. Documents is split by stage so each group reads as
# one step of the flow; unlisted tags fall in after these.
OPENAPI_TAGS = [
    {"name": "Authentication"},
    {"name": "Users"},
    {"name": "Books", "description": "Notarial register books."},
    {"name": "Documents", "description": "List, read, rename, history."},
    {
        "name": "Documents · Preparation (Path 1)",
        "description": "Open a record and work on the draft in Google Docs "
        "until it is ready for signature.",
    },
    {
        "name": "Documents · Signed copy & register",
        "description": "Attach (Path 1) or upload (Path 2) the signed PDF and "
        "file it in a book. Replace or correct before finalize.",
    },
    {
        "name": "Documents · Finalize & verify",
        "description": "Freeze the signed copy, anchor its hash, verify it later.",
    },
    {
        "name": "Documents · OCR review",
        "description": "After finalize: review the extracted text for search.",
    },
    {"name": "Documents · Sharing", "description": "Parties and invitations."},
    {
        "name": "Documents · E-copy requests",
        "description": "A party asks for the unwatermarked signed copy; the "
        "owner approves (and it is emailed) or declines.",
    },
    {"name": "Retrieval"},
    {"name": "Notifications"},
    {"name": "Admin"},
]

app = FastAPI(lifespan=lifespan, openapi_tags=OPENAPI_TAGS)

# The front-end runs on its own origin, so without this every browser call is
# blocked. Bearer tokens travel in the Authorization header, not cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"],
)


@app.middleware("http")
async def langfuse_flush_middleware(request: Request, call_next):
    response = await call_next(request)  # full request runs, @observe closes span
    await asyncio.to_thread(get_client().flush)  # flush AFTER span is fully closed
    return response


@app.exception_handler(BaseAppException)
async def app_exception_handler(request: Request, exc: BaseAppException):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
        headers=exc.headers,
    )


# Added 2026-09-07 23:06 PST — render framework errors in the app's own shape.
# The handler above only covers exceptions this codebase raises, so three kinds
# of error still escaped in FastAPI's {"detail": ...} shape: a missing or
# malformed Authorization header (HTTPBearer rejects it before any of our code
# runs), an unmatched route or wrong method (Starlette's router), and body
# validation (Pydantic). The 422 was the expensive one — there `detail` is a
# *list* of error objects, not a string, so a client that read `detail` for the
# other two rendered "[object Object]" for the error users hit most often.
# These have no raise site of ours to fix; the handler is the only place they
# pass through, which is why the normalization lives here.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Registered on Starlette's class, not fastapi.HTTPException, which
    # subclasses it: the router raises the parent directly, and handler lookup
    # walks the MRO, so the parent catches both. Registering the subclass would
    # miss every 404/405.
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_response(
            error_type=HTTP_ERROR_TYPES.get(exc.status_code, "HTTPError"),
            message=str(exc.detail),
            status_code=exc.status_code,
        ),
        # HTTPBearer's 401 carries WWW-Authenticate; passing it through is what
        # lets get_current_user keep using auto_error=True and still answer in
        # this shape. Starlette's base sets headers to None when unused.
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=build_error_response(
            error_type="ValidationError",
            message="Request validation failed",
            status_code=422,
            # jsonable_encoder because the per-field errors can hold values
            # JSONResponse cannot serialize on its own (ValueError, bytes).
            details={"errors": jsonable_encoder(exc.errors())},
        ),
    )


# Added 2026-09-07 23:06 PST — last resort so a 500 is not plain text.
# Starlette answers an unhandled exception with the literal body "Internal
# Server Error", which is the one response no client could parse at all.
# Details stay out of the body on purpose: an unhandled error is by definition
# one whose message was never written for a user, and it can carry SQL or
# tokens. Starlette re-raises after this returns, so uvicorn still logs the
# traceback — the log call here is what attaches the request path to it.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception(
        "request.unhandled_exception",
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=500,
        content=build_error_response(
            error_type="InternalServerError",
            message="An unexpected error occurred.",
            status_code=500,
        ),
    )


app.include_router(auth_router)
app.include_router(user_router)
app.include_router(document_router)
app.include_router(retrieval_router)
# app.include_router(blockchain_router)
app.include_router(admin_router)
app.include_router(notification_router)
app.include_router(book_router)
app.include_router(google_router)
