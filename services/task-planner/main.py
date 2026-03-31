import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("task-planner")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://screenmind:screenmind@localhost:5432/screenmind")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
LLM_REASONING_URL = os.getenv("LLM_REASONING_URL", "http://llm-reasoning:8005")
PLAN_CACHE_TTL = int(os.getenv("PLAN_CACHE_TTL", "3600"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
TASK_CREATED = Counter("tasks_created_total", "Total tasks created")
TASK_STATUS_CHANGES = Counter("task_status_changes_total", "Task status changes", ["new_status"])
PLAN_LATENCY = Histogram("plan_generation_latency_seconds", "Time to generate a task plan")

# ---------------------------------------------------------------------------
# Enums & Pydantic models
# ---------------------------------------------------------------------------
class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CreateTaskRequest(BaseModel):
    task_description: str
    user_id: str


class UpdateStatusRequest(BaseModel):
    status: TaskStatus
    error_message: str | None = None


class PlanRequest(BaseModel):
    task_id: str
    goal: str
    screen_state: dict[str, Any] = Field(default_factory=dict)
    reasoning: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
async def get_db_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                description TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'PENDING',
                error_message TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_steps (
                id          SERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                step_number INTEGER NOT NULL,
                description TEXT NOT NULL,
                action      JSONB NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------
async def get_redis() -> aioredis.Redis:
    return await aioredis.from_url(REDIS_URL, decode_responses=True)


def plan_cache_key(task_id: str) -> str:
    return f"plan:{task_id}"


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
app = FastAPI(title="Task Planner Service", version="1.0.0")
db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


@app.on_event("startup")
async def startup():
    global db_pool, redis_client
    try:
        db_pool = await get_db_pool()
        await ensure_schema(db_pool)
        logger.info("PostgreSQL connected")
    except Exception as exc:
        logger.warning("PostgreSQL unavailable at startup: %s", exc)

    try:
        redis_client = await get_redis()
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as exc:
        logger.warning("Redis unavailable at startup: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def row_to_task(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "task_id": row["id"],
        "user_id": row["user_id"],
        "description": row["description"],
        "status": row["status"],
        "error_message": row["error_message"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def require_db() -> asyncpg.Pool:
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return db_pool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    db_ok = db_pool is not None
    redis_ok = redis_client is not None
    return {"status": "healthy", "database": db_ok, "redis": redis_ok}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/tasks", status_code=201)
async def create_task(req: CreateTaskRequest):
    pool = require_db()
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tasks (id, user_id, description, status, created_at, updated_at)
            VALUES ($1, $2, $3, 'PENDING', $4, $4)
            RETURNING *
            """,
            task_id, req.user_id, req.task_description, now,
        )
    TASK_CREATED.inc()
    logger.info("Task created task_id=%s user_id=%s", task_id, req.user_id)
    return row_to_task(row)


@app.get("/tasks")
async def list_tasks(user_id: str | None = Query(None)):
    pool = require_db()
    async with pool.acquire() as conn:
        if user_id:
            rows = await conn.fetch("SELECT * FROM tasks WHERE user_id = $1 ORDER BY created_at DESC", user_id)
        else:
            rows = await conn.fetch("SELECT * FROM tasks ORDER BY created_at DESC")
    return [row_to_task(r) for r in rows]


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    pool = require_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row_to_task(row)


@app.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    pool = require_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM tasks WHERE id = $1", task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": row["status"]}


@app.put("/tasks/{task_id}/status")
async def update_task_status(task_id: str, req: UpdateStatusRequest):
    pool = require_db()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE tasks SET status = $1, error_message = $2, updated_at = $3
            WHERE id = $4 RETURNING *
            """,
            req.status.value, req.error_message, now, task_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    TASK_STATUS_CHANGES.labels(new_status=req.status.value).inc()
    logger.info("Task %s status -> %s", task_id, req.status.value)
    return row_to_task(row)


@app.delete("/tasks/{task_id}", status_code=200)
async def cancel_task(task_id: str):
    pool = require_db()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE tasks SET status = 'CANCELLED', updated_at = $1 WHERE id = $2 RETURNING *",
            now, task_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    logger.info("Task %s cancelled", task_id)
    return row_to_task(row)


@app.post("/plan")
async def create_plan(req: PlanRequest):
    start = time.perf_counter()

    # Return cached plan if available
    if redis_client:
        try:
            cached = await redis_client.get(plan_cache_key(req.task_id))
            if cached:
                logger.info("Returning cached plan for task_id=%s", req.task_id)
                return json.loads(cached)
        except Exception as exc:
            logger.warning("Redis get error: %s", exc)

    # Build a planning prompt and call the LLM reasoning service
    planning_screen_state = {
        "screen_type": req.screen_state.get("screen_type", "unknown"),
        "state_summary": req.screen_state.get("state_summary", ""),
        "key_text": req.screen_state.get("key_text", []),
        "interactive_elements": req.screen_state.get("interactive_elements", []),
        "ocr_text": req.screen_state.get("ocr_text", ""),
    }

    llm_payload = {
        "task_id": req.task_id,
        "goal": f"Create a step-by-step plan to accomplish: {req.goal}. "
                "Return a JSON object with a 'steps' array where each step has "
                "'step_number' (int), 'description' (str), and 'action' (dict with action_type).",
        "screen_state": planning_screen_state,
        "step_number": 0,
        "memory_context": [],
        "previous_actions": [],
    }

    steps: list[dict[str, Any]] = []
    model_used = "unknown"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{LLM_REASONING_URL}/reason", json=llm_payload)
            resp.raise_for_status()
            llm_result = resp.json()

        model_used = llm_result.get("model_used", "unknown")
        next_action = llm_result.get("next_action", {})
        decision = llm_result.get("decision", req.goal)

        # Try to extract structured steps from the LLM decision text
        try:
            decision_data = json.loads(decision) if decision.strip().startswith("{") else {}
            raw_steps = decision_data.get("steps", [])
        except (json.JSONDecodeError, AttributeError):
            raw_steps = []

        if raw_steps:
            steps = [
                {
                    "step_number": s.get("step_number", i + 1),
                    "description": s.get("description", ""),
                    "action": s.get("action", {}),
                }
                for i, s in enumerate(raw_steps)
            ]
        else:
            # Fall back to a single-step plan from the reasoning result
            steps = [
                {
                    "step_number": 1,
                    "description": decision,
                    "action": next_action,
                }
            ]

    except Exception as exc:
        logger.error("LLM reasoning call failed during planning: %s", exc)
        # Minimal fallback plan
        steps = [
            {
                "step_number": 1,
                "description": f"Take a screenshot to assess the current state for: {req.goal}",
                "action": {"action_type": "TAKE_SCREENSHOT"},
            }
        ]

    plan = {
        "task_id": req.task_id,
        "goal": req.goal,
        "steps": steps,
        "estimated_steps": len(steps),
        "model_used": model_used,
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }

    # Persist steps to DB if available
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                # Remove any old steps for this task
                await conn.execute("DELETE FROM task_steps WHERE task_id = $1", req.task_id)
                for step in steps:
                    await conn.execute(
                        """
                        INSERT INTO task_steps (task_id, step_number, description, action)
                        VALUES ($1, $2, $3, $4)
                        """,
                        req.task_id,
                        step["step_number"],
                        step["description"],
                        json.dumps(step["action"]),
                    )
        except Exception as exc:
            logger.warning("Failed to persist steps to DB: %s", exc)

    # Cache the plan in Redis
    if redis_client:
        try:
            await redis_client.setex(plan_cache_key(req.task_id), PLAN_CACHE_TTL, json.dumps(plan))
        except Exception as exc:
            logger.warning("Redis set error: %s", exc)

    PLAN_LATENCY.observe(time.perf_counter() - start)
    logger.info("Plan created for task_id=%s with %d steps", req.task_id, len(steps))
    return plan


@app.get("/tasks/{task_id}/steps")
async def get_task_steps(task_id: str):
    # Try Redis cache first
    if redis_client:
        try:
            cached = await redis_client.get(plan_cache_key(task_id))
            if cached:
                data = json.loads(cached)
                return {"task_id": task_id, "steps": data.get("steps", []), "source": "cache"}
        except Exception as exc:
            logger.warning("Redis get error: %s", exc)

    # Fall back to DB
    pool = require_db()
    async with pool.acquire() as conn:
        # Verify task exists
        task_row = await conn.fetchrow("SELECT id FROM tasks WHERE id = $1", task_id)
        if not task_row:
            raise HTTPException(status_code=404, detail="Task not found")
        rows = await conn.fetch(
            "SELECT step_number, description, action FROM task_steps WHERE task_id = $1 ORDER BY step_number",
            task_id,
        )

    steps = [
        {
            "step_number": r["step_number"],
            "description": r["description"],
            "action": json.loads(r["action"]) if isinstance(r["action"], str) else r["action"],
        }
        for r in rows
    ]
    return {"task_id": task_id, "steps": steps, "source": "database"}
