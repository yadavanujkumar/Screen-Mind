"""Observability Service - Logging, metrics, and Prometheus exposition."""
import json as _json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx

import asyncpg
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/screenmind")
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "")

# ── Prometheus metrics ────────────────────────────────────────────────────────
task_success_rate = Gauge("task_success_rate", "Overall task success rate")
action_success_rate = Gauge("action_success_rate", "Per-action success rate")
avg_task_completion_time = Histogram(
    "avg_task_completion_time_seconds",
    "Task completion time in seconds",
    buckets=[1, 5, 15, 30, 60, 120, 300, 600],
)
llm_latency_seconds = Histogram(
    "llm_latency_seconds",
    "LLM inference latency in seconds",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
error_rate = Counter("error_rate_total", "Total number of errors recorded")
steps_per_task = Histogram(
    "steps_per_task",
    "Number of steps taken per task",
    buckets=[1, 2, 5, 10, 20, 50, 100],
)

# Global DB pool
db_pool: Optional[asyncpg.Pool] = None


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                service_name TEXT NOT NULL,
                log_level TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_metrics (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_number INT NOT NULL,
                step_time FLOAT,
                model_latency FLOAT,
                success_rate FLOAT,
                recorded_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(log_level)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_task ON task_metrics(task_id)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db(db_pool)
    logger.info("Observability service ready")
    yield
    await db_pool.close()
    logger.info("Observability service shut down")


app = FastAPI(title="Observability Service", version="1.0.0", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────

class LogRequest(BaseModel):
    task_id: str
    service_name: str
    log_level: str = "INFO"
    message: str
    metadata: dict = Field(default_factory=dict)


class MetricsRequest(BaseModel):
    task_id: str
    step_number: int
    step_time: float = 0.0
    model_latency: float = 0.0
    success_rate: float = 1.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def push_to_elasticsearch(index: str, doc: dict):
    """Push a document to Elasticsearch if configured."""
    if not ELASTICSEARCH_URL:
        return
    url = f"{ELASTICSEARCH_URL}/{index}/_doc"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=doc)
    except Exception as exc:
        logger.warning("Elasticsearch push failed: %s", exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/log", status_code=201)
async def store_log(req: LogRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO logs (task_id, service_name, log_level, message, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING *
            """,
            req.task_id,
            req.service_name,
            req.log_level.upper(),
            req.message,
            _json.dumps(req.metadata),
        )

    if req.log_level.upper() == "ERROR":
        error_rate.inc()

    doc = row_to_dict(row)
    await push_to_elasticsearch("screenmind-logs", doc)

    logger.info("Log stored id=%d task=%s level=%s", row["id"], req.task_id, req.log_level)
    return doc


@app.post("/metrics", status_code=201)
async def store_metrics(req: MetricsRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO task_metrics (task_id, step_number, step_time, model_latency, success_rate)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            req.task_id,
            req.step_number,
            req.step_time,
            req.model_latency,
            req.success_rate,
        )

    # Update Prometheus metrics
    action_success_rate.set(req.success_rate)
    if req.step_time > 0:
        avg_task_completion_time.observe(req.step_time)
    if req.model_latency > 0:
        llm_latency_seconds.observe(req.model_latency)
    if req.step_number > 0:
        steps_per_task.observe(req.step_number)

    logger.info("Metrics stored task=%s step=%d", req.task_id, req.step_number)
    return row_to_dict(row)


@app.get("/metrics/{task_id}")
async def get_task_metrics(task_id: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM task_metrics WHERE task_id = $1 ORDER BY step_number",
            task_id,
        )
    return {"task_id": task_id, "metrics": [row_to_dict(r) for r in rows], "total": len(rows)}


@app.get("/logs/{task_id}")
async def get_task_logs(task_id: str, log_level: Optional[str] = Query(default=None)):
    async with db_pool.acquire() as conn:
        if log_level:
            rows = await conn.fetch(
                "SELECT * FROM logs WHERE task_id = $1 AND log_level = $2 ORDER BY created_at",
                task_id,
                log_level.upper(),
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM logs WHERE task_id = $1 ORDER BY created_at",
                task_id,
            )
    return {"task_id": task_id, "logs": [row_to_dict(r) for r in rows], "total": len(rows)}


@app.get("/metrics/summary")
async def metrics_summary():
    async with db_pool.acquire() as conn:
        summary = await conn.fetchrow("""
            SELECT
                AVG(success_rate)   AS avg_success_rate,
                AVG(model_latency)  AS avg_model_latency,
                AVG(step_time)      AS avg_step_time,
                COUNT(DISTINCT task_id) AS total_tasks,
                COUNT(*)            AS total_steps
            FROM task_metrics
        """)
        error_count = await conn.fetchval(
            "SELECT COUNT(*) FROM logs WHERE log_level = 'ERROR'"
        )

    return {
        "avg_success_rate": float(summary["avg_success_rate"] or 0),
        "avg_model_latency_seconds": float(summary["avg_model_latency"] or 0),
        "avg_step_time_seconds": float(summary["avg_step_time"] or 0),
        "total_tasks": summary["total_tasks"],
        "total_steps": summary["total_steps"],
        "total_errors": error_count,
    }


@app.get("/prometheus", response_class=PlainTextResponse)
async def prometheus_metrics():
    return PlainTextResponse(
        generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/health")
async def health():
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    return {
        "status": "ok",
        "database": db_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8010, reload=False)
