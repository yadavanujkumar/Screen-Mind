"""
Screen-Mind Task Queue Service
FastAPI service (port 8012) using Redis sorted sets as a priority queue.
"""

import asyncio
import time
from functools import partial
from typing import Optional

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# ---------------------------------------------------------------------------
# App & Redis setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Screen-Mind Task Queue", version="1.0.0")

QUEUE_KEY = "screenmind:task_queue"
PROCESSING_KEY = "screenmind:processing"
TASK_DATA_PREFIX = "screenmind:task:"

_redis_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)
    return _redis_client


async def run_in_executor(func, *args):
    """Run a synchronous Redis call in a thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args))


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

tasks_enqueued = Counter("task_queue_enqueued_total", "Total tasks enqueued")
tasks_dequeued = Counter("task_queue_dequeued_total", "Total tasks dequeued")
tasks_cancelled = Counter("task_queue_cancelled_total", "Total tasks cancelled")
queue_length_gauge = Gauge("task_queue_length", "Current queue length")
processing_gauge = Gauge("task_queue_processing", "Tasks currently being processed")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EnqueueRequest(BaseModel):
    task_id: str
    user_id: str
    task_description: str
    priority: int = Field(default=5, ge=1, le=10)


class EnqueueResponse(BaseModel):
    queued: bool
    position: int
    task_id: str


class DequeueResponse(BaseModel):
    task_id: Optional[str] = None
    task_description: Optional[str] = None
    user_id: Optional[str] = None


class QueueStatusResponse(BaseModel):
    queue_length: int
    processing: int
    workers: int


class CancelResponse(BaseModel):
    cancelled: bool
    task_id: str


class HealthResponse(BaseModel):
    status: str
    redis: str
    queue_length: int


# ---------------------------------------------------------------------------
# Helper – compute score
# ---------------------------------------------------------------------------

def _compute_score(priority: int) -> float:
    """
    Score = -(priority * 1e12) + timestamp_ms so that:
      - higher priority → lower score → popped first (ZPOPMIN)
      - ties broken by insertion time (earlier = lower score = popped first)
    """
    ts = int(time.time() * 1000)
    return -(priority * 1_000_000_000_000) + ts


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/enqueue", response_model=EnqueueResponse)
async def enqueue(request: EnqueueRequest):
    r = get_redis()
    score = _compute_score(request.priority)

    task_data = {
        "task_id": request.task_id,
        "user_id": request.user_id,
        "task_description": request.task_description,
        "priority": str(request.priority),
        "enqueued_at": str(time.time()),
        "status": "queued",
    }

    def _enqueue():
        pipe = r.pipeline()
        # Store task metadata
        pipe.hset(f"{TASK_DATA_PREFIX}{request.task_id}", mapping=task_data)
        # Add to priority sorted set
        pipe.zadd(QUEUE_KEY, {request.task_id: score})
        pipe.execute()
        # Position = rank in sorted set (0-indexed)
        rank = r.zrank(QUEUE_KEY, request.task_id)
        return rank

    try:
        rank = await run_in_executor(_enqueue)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

    position = (rank or 0) + 1
    tasks_enqueued.inc()
    queue_length_gauge.set(await run_in_executor(r.zcard, QUEUE_KEY))

    return EnqueueResponse(queued=True, position=position, task_id=request.task_id)


@app.post("/dequeue", response_model=DequeueResponse)
async def dequeue():
    r = get_redis()

    def _dequeue():
        # Atomically pop the highest-priority (lowest score) task
        results = r.zpopmin(QUEUE_KEY, count=1)
        if not results:
            return None
        task_id, _score = results[0]
        # Mark as processing
        r.sadd(PROCESSING_KEY, task_id)
        r.hset(f"{TASK_DATA_PREFIX}{task_id}", "status", "processing")
        data = r.hgetall(f"{TASK_DATA_PREFIX}{task_id}")
        return data

    try:
        data = await run_in_executor(_dequeue)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

    if not data:
        return DequeueResponse(task_id=None)

    tasks_dequeued.inc()
    queue_length_gauge.set(await run_in_executor(r.zcard, QUEUE_KEY))
    processing_count = await run_in_executor(r.scard, PROCESSING_KEY)
    processing_gauge.set(processing_count)

    return DequeueResponse(
        task_id=data.get("task_id"),
        task_description=data.get("task_description"),
        user_id=data.get("user_id"),
    )


@app.get("/queue/status", response_model=QueueStatusResponse)
async def queue_status():
    r = get_redis()
    try:
        queue_length = await run_in_executor(r.zcard, QUEUE_KEY)
        processing = await run_in_executor(r.scard, PROCESSING_KEY)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

    queue_length_gauge.set(queue_length)
    processing_gauge.set(processing)

    return QueueStatusResponse(
        queue_length=queue_length,
        processing=processing,
        workers=1,  # configurable via env in a real deployment
    )


@app.post("/queue/task/{task_id}/cancel", response_model=CancelResponse)
async def cancel_task(task_id: str):
    r = get_redis()

    def _cancel():
        removed = r.zrem(QUEUE_KEY, task_id)
        if removed:
            r.hset(f"{TASK_DATA_PREFIX}{task_id}", "status", "cancelled")
        return removed

    try:
        removed = await run_in_executor(_cancel)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

    if not removed:
        # Task may already be processing or doesn't exist
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found in queue (may already be processing or completed).",
        )

    tasks_cancelled.inc()
    queue_length_gauge.set(await run_in_executor(r.zcard, QUEUE_KEY))
    return CancelResponse(cancelled=True, task_id=task_id)


@app.get("/health", response_model=HealthResponse)
async def health():
    r = get_redis()
    try:
        r.ping()
        redis_status = "ok"
        queue_length = await run_in_executor(r.zcard, QUEUE_KEY)
    except redis.RedisError:
        redis_status = "unavailable"
        queue_length = -1

    return HealthResponse(
        status="ok" if redis_status == "ok" else "degraded",
        redis=redis_status,
        queue_length=queue_length,
    )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
