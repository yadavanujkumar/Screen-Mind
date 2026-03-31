"""Explainability Service - Store and retrieve AI decision logs for each task step."""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/screenmind")

db_pool: Optional[asyncpg.Pool] = None


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS explainability_logs (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_number INT NOT NULL,
                screen_text TEXT,
                detected_elements JSONB DEFAULT '[]',
                goal TEXT,
                decision TEXT,
                reason TEXT,
                alternatives JSONB DEFAULT '[]',
                confidence_score FLOAT DEFAULT 0.0,
                what_ai_saw TEXT,
                what_ai_understood TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (task_id, step_number)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_explain_task ON explainability_logs(task_id)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db(db_pool)
    logger.info("Explainability service ready")
    yield
    await db_pool.close()
    logger.info("Explainability service shut down")


app = FastAPI(title="Explainability Service", version="1.0.0", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ExplainRequest(BaseModel):
    task_id: str
    step_number: int = Field(ge=0)
    screen_text: str = ""
    detected_elements: list = Field(default_factory=list)
    goal: str = ""
    decision: str = ""
    reason: str = ""
    alternatives: list = Field(default_factory=list)
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    what_ai_saw: str = ""
    what_ai_understood: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

import json as _json


def row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, str) and k in ("detected_elements", "alternatives"):
            # Older asyncpg versions may return JSONB as a raw string
            try:
                d[k] = _json.loads(v)
            except Exception:
                pass
        # If already list/dict (newer asyncpg), leave as-is
    return d


def format_step_report(row: dict, step_idx: int) -> str:
    """Format a single step as a human-readable block."""
    lines = [
        f"{'='*60}",
        f"  STEP {row.get('step_number', step_idx)}",
        f"{'='*60}",
        f"Goal         : {row.get('goal', 'N/A')}",
        f"Confidence   : {row.get('confidence_score', 0):.1%}",
        "",
        "What the AI Saw:",
        f"  {row.get('what_ai_saw', 'N/A')}",
        "",
        "What the AI Understood:",
        f"  {row.get('what_ai_understood', 'N/A')}",
        "",
        "Decision:",
        f"  {row.get('decision', 'N/A')}",
        "",
        "Reason:",
        f"  {row.get('reason', 'N/A')}",
    ]

    alternatives = row.get("alternatives") or []
    if alternatives:
        lines.append("")
        lines.append("Alternatives Considered:")
        for alt in alternatives:
            lines.append(f"  - {alt}")

    detected = row.get("detected_elements") or []
    if detected:
        lines.append("")
        lines.append(f"Detected Elements: {len(detected)} element(s)")

    lines.append("")
    return "\n".join(lines)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/explain", status_code=201)
async def store_explanation(req: ExplainRequest):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO explainability_logs
                (task_id, step_number, screen_text, detected_elements, goal, decision,
                 reason, alternatives, confidence_score, what_ai_saw, what_ai_understood)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8::jsonb, $9, $10, $11)
            ON CONFLICT (task_id, step_number) DO UPDATE SET
                screen_text       = EXCLUDED.screen_text,
                detected_elements = EXCLUDED.detected_elements,
                goal              = EXCLUDED.goal,
                decision          = EXCLUDED.decision,
                reason            = EXCLUDED.reason,
                alternatives      = EXCLUDED.alternatives,
                confidence_score  = EXCLUDED.confidence_score,
                what_ai_saw       = EXCLUDED.what_ai_saw,
                what_ai_understood = EXCLUDED.what_ai_understood,
                created_at        = NOW()
            RETURNING *
            """,
            req.task_id,
            req.step_number,
            req.screen_text,
            _json.dumps(req.detected_elements),
            req.goal,
            req.decision,
            req.reason,
            _json.dumps(req.alternatives),
            req.confidence_score,
            req.what_ai_saw,
            req.what_ai_understood,
        )

    logger.info("Explanation stored task=%s step=%d", req.task_id, req.step_number)
    return row_to_dict(row)


@app.get("/explain/{task_id}")
async def get_task_explanations(task_id: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM explainability_logs WHERE task_id = $1 ORDER BY step_number",
            task_id,
        )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No explanations found for task '{task_id}'")
    return {
        "task_id": task_id,
        "steps": [row_to_dict(r) for r in rows],
        "total_steps": len(rows),
    }


@app.get("/explain/{task_id}/{step_number}")
async def get_step_explanation(task_id: str, step_number: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM explainability_logs WHERE task_id = $1 AND step_number = $2",
            task_id,
            step_number,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No explanation for task '{task_id}' step {step_number}",
        )
    return row_to_dict(row)


@app.get("/explain/{task_id}/report", response_class=PlainTextResponse)
async def get_task_report(task_id: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM explainability_logs WHERE task_id = $1 ORDER BY step_number",
            task_id,
        )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No explanations found for task '{task_id}'")

    header = [
        f"{'#'*60}",
        f"  TASK DECISION CHAIN REPORT",
        f"  Task ID : {task_id}",
        f"  Steps   : {len(rows)}",
        f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"{'#'*60}",
        "",
    ]

    step_blocks = []
    for i, row in enumerate(rows):
        step_blocks.append(format_step_report(row_to_dict(row), i + 1))

    # Summary
    avg_confidence = sum(float(r["confidence_score"] or 0) for r in rows) / len(rows)
    footer = [
        f"{'='*60}",
        f"  SUMMARY",
        f"{'='*60}",
        f"  Total Steps      : {len(rows)}",
        f"  Avg Confidence   : {avg_confidence:.1%}",
        "",
    ]

    report = "\n".join(header) + "\n".join(step_blocks) + "\n".join(footer)
    return PlainTextResponse(report, media_type="text/plain")


@app.get("/health")
async def health():
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        db_status = "error: database unreachable"

    return {
        "status": "ok",
        "database": db_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8011, reload=False)
