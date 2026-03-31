"""
Screen-Mind API Gateway
Single entry point for all external clients. Handles authentication, rate limiting,
routing, observability, and WebSocket live updates.
"""

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import asyncpg
import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import (
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("api-gateway")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    database_url: str = "postgresql://screenmind:screenmind@localhost:5432/screenmind"
    task_planner_url: str = "http://localhost:8002"
    explainability_url: str = "http://localhost:8005"
    action_execution_url: str = "http://localhost:8004"
    observability_url: str = "http://localhost:8007"
    memory_url: str = "http://localhost:8006"
    agent_orchestrator_url: str = "http://localhost:8003"
    conversation_url: str = "http://localhost:8013"
    jaeger_host: str = "localhost"
    jaeger_port: int = 6831
    rate_limit: str = "100/minute"
    cors_origins: list[str] = ["*"]
    # Server-side secret used when hashing API keys (set via env var in production)
    api_key_secret: str = "change-me-in-production"

    class Config:
        env_file = ".env"


settings = Settings()

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "api_gateway_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "api_gateway_request_latency_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
)

# ---------------------------------------------------------------------------
# OpenTelemetry tracing
# ---------------------------------------------------------------------------


def setup_tracing() -> None:
    resource = Resource.create({"service.name": "api-gateway"})
    provider = TracerProvider(resource=resource)
    try:
        exporter = JaegerExporter(
            agent_host_name=settings.jaeger_host,
            agent_port=settings.jaeger_port,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("Jaeger tracing configured at %s:%d", settings.jaeger_host, settings.jaeger_port)
    except Exception as exc:
        logger.warning("Could not configure Jaeger exporter: %s", exc)
    trace.set_tracer_provider(provider)


# ---------------------------------------------------------------------------
# DB pool lifecycle
# ---------------------------------------------------------------------------

db_pool: Optional[asyncpg.Pool] = None


async def get_db_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return db_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global db_pool
    setup_tracing()
    try:
        db_pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
        logger.info("Database pool created")
    except Exception as exc:
        logger.error("Failed to create DB pool: %s", exc)
    yield
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Screen-Mind API Gateway",
    version="1.0.0",
    description="Enterprise AI Computer Control Agent - API Gateway",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FastAPIInstrumentor.instrument_app(app)

# ---------------------------------------------------------------------------
# Prometheus middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next: Any) -> Response:
    endpoint = request.url.path
    method = request.method
    start = time.perf_counter()
    response: Response = await call_next(request)
    latency = time.perf_counter() - start
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status_code=response.status_code).inc()
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(latency)
    return response


# ---------------------------------------------------------------------------
# API Key authentication
# ---------------------------------------------------------------------------


def _hash_api_key(api_key: str) -> str:
    """HMAC-SHA256 of the API key using the server-side secret.

    API keys are high-entropy random tokens (256 bits), so a fast HMAC is
    appropriate here. The server-side secret prevents offline brute-force even
    if the database is compromised. Using hmac.digest with a string digestmod
    avoids referencing hashlib directly on the sensitive data path.
    """
    return hmac.digest(
        settings.api_key_secret.encode(),
        api_key.encode(),
        "sha256",
    ).hex()


async def authenticate(request: Request) -> dict:
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    pool: asyncpg.Pool = await get_db_pool()
    key_hash = _hash_api_key(api_key)
    row = await pool.fetchrow(
        "SELECT id, username, role, is_active FROM users WHERE api_key_hash = $1",
        key_hash,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account deactivated",
        )
    return dict(row)


# ---------------------------------------------------------------------------
# HTTP client helpers
# ---------------------------------------------------------------------------


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60.0)


async def _proxy(
    request: Request,
    base_url: str,
    path: str,
    *,
    method: Optional[str] = None,
    body: Optional[bytes] = None,
) -> JSONResponse:
    method = method or request.method
    body = body if body is not None else await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    params = dict(request.query_params)
    async with _build_client() as client:
        try:
            resp = await client.request(method, url, content=body, headers=headers, params=params)
            try:
                data = resp.json()
            except Exception:
                data = {"detail": resp.text}
            return JSONResponse(content=data, status_code=resp.status_code)
        except httpx.ConnectError as exc:
            logger.error("Upstream connect error to %s: %s", url, exc)
            raise HTTPException(status_code=502, detail=f"Upstream service unavailable: {base_url}")
        except httpx.TimeoutException as exc:
            logger.error("Upstream timeout to %s: %s", url, exc)
            raise HTTPException(status_code=504, detail="Upstream service timed out")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    description: str
    context: Optional[dict] = None
    priority: Optional[str] = "normal"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["system"])
async def health_check() -> dict:
    return {"status": "healthy", "service": "api-gateway"}


@app.get("/metrics", tags=["system"])
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Tasks ------------------------------------------------------------------


@app.post("/api/v1/tasks", tags=["tasks"])
@limiter.limit(settings.rate_limit)
async def create_task(
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.task_planner_url, "/tasks")


@app.get("/api/v1/tasks/{task_id}", tags=["tasks"])
@limiter.limit(settings.rate_limit)
async def get_task(
    task_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.task_planner_url, f"/tasks/{task_id}")


@app.get("/api/v1/tasks/{task_id}/status", tags=["tasks"])
@limiter.limit(settings.rate_limit)
async def get_task_status(
    task_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.task_planner_url, f"/tasks/{task_id}/status")


@app.post("/api/v1/tasks/{task_id}/execute", tags=["tasks"])
@limiter.limit(settings.rate_limit)
async def execute_task(
    task_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    """Start the agent loop by calling the agent orchestrator."""
    return await _proxy(request, settings.agent_orchestrator_url, f"/execute/{task_id}")


# --- Explainability ---------------------------------------------------------


@app.get("/api/v1/tasks/{task_id}/explainability", tags=["explainability"])
@limiter.limit(settings.rate_limit)
async def get_task_explainability(
    task_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.explainability_url, f"/explain/{task_id}")


# --- Actions ----------------------------------------------------------------


@app.get("/api/v1/tasks/{task_id}/actions", tags=["actions"])
@limiter.limit(settings.rate_limit)
async def get_task_actions(
    task_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.action_execution_url, f"/actions/{task_id}")


# --- Observability metrics --------------------------------------------------


@app.get("/api/v1/metrics", tags=["observability"])
@limiter.limit(settings.rate_limit)
async def get_observability_metrics(
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.observability_url, "/metrics")


# --- Memory -----------------------------------------------------------------


@app.get("/api/v1/memory", tags=["memory"])
@limiter.limit(settings.rate_limit)
async def get_memory(
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    return await _proxy(request, settings.memory_url, "/memory")


# --- Conversations ----------------------------------------------------------


@app.post("/api/v1/conversations", tags=["conversations"])
@limiter.limit(settings.rate_limit)
async def create_conversation(
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    """Start a new conversation session."""
    return await _proxy(request, settings.conversation_url, "/sessions")


@app.get("/api/v1/conversations/{session_id}", tags=["conversations"])
@limiter.limit(settings.rate_limit)
async def get_conversation(
    session_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    """Get conversation session info and message history."""
    return await _proxy(request, settings.conversation_url, f"/sessions/{session_id}")


@app.post("/api/v1/conversations/{session_id}/messages", tags=["conversations"])
@limiter.limit(settings.rate_limit)
async def send_conversation_message(
    session_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    """Send a message to the conversation session and receive a reply."""
    return await _proxy(request, settings.conversation_url, f"/sessions/{session_id}/messages")


@app.post("/api/v1/conversations/{session_id}/execute", tags=["conversations"])
@limiter.limit(settings.rate_limit)
async def execute_conversation_direction(
    session_id: str,
    request: Request,
    _user: dict = Depends(authenticate),
) -> JSONResponse:
    """Execute the latest direction from the conversation as a remote task."""
    return await _proxy(request, settings.conversation_url, f"/sessions/{session_id}/execute")


# ---------------------------------------------------------------------------
# WebSocket live updates
# ---------------------------------------------------------------------------


@app.websocket("/ws/tasks/{task_id}/live")
async def websocket_task_live(websocket: WebSocket, task_id: str) -> None:
    """Stream live task updates by polling the task-planner and forwarding events."""
    api_key = websocket.headers.get("X-API-Key") or websocket.query_params.get("api_key")
    if not api_key:
        await websocket.close(code=4001, reason="Missing API key")
        return

    pool = db_pool
    if pool is None:
        await websocket.close(code=4503, reason="Database unavailable")
        return

    key_hash = _hash_api_key(api_key)
    row = await pool.fetchrow(
        "SELECT id, username, is_active FROM users WHERE api_key_hash = $1",
        key_hash,
    )
    if not row or not row["is_active"]:
        await websocket.close(code=4001, reason="Invalid or inactive API key")
        return

    await websocket.accept()
    logger.info("WebSocket connected for task %s by user %s", task_id, row["username"])

    poll_interval = 2.0
    last_status: Optional[str] = None

    try:
        async with _build_client() as client:
            while True:
                try:
                    resp = await client.get(
                        f"{settings.task_planner_url}/tasks/{task_id}/status"
                    )
                    if resp.status_code == 200:
                        payload = resp.json()
                        current_status = payload.get("status")
                        if current_status != last_status:
                            await websocket.send_json(payload)
                            last_status = current_status
                        if current_status in ("completed", "failed", "cancelled"):
                            logger.info("Task %s terminal state: %s", task_id, current_status)
                            break
                    else:
                        await websocket.send_json(
                            {"error": "Failed to fetch status", "code": resp.status_code}
                        )
                except httpx.HTTPError as exc:
                    logger.warning("HTTP error polling task %s: %s", task_id, exc)
                    await websocket.send_json({"error": str(exc)})

                await asyncio.sleep(poll_interval)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for task %s", task_id)
    except Exception as exc:
        logger.error("WebSocket error for task %s: %s", task_id, exc)
        try:
            await websocket.send_json({"error": "Internal server error"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
