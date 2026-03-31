import asyncio
import json
import logging
import os
import subprocess
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
import pyautogui
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("action-execution")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://screenmind:screenmind@localhost:5432/screenmind")
SCREEN_CAPTURE_URL = os.getenv("SCREEN_CAPTURE_URL", "http://screen-capture:8002")
SAFE_MODE = os.getenv("SAFE_MODE", "false").lower() == "true"
BLOCK_DANGEROUS_KEYS = os.getenv("BLOCK_DANGEROUS_KEYS", "true").lower() == "true"

_allowed_env = os.getenv("ALLOWED_ACTIONS", "")
ALLOWED_ACTIONS: set[str] | None = (
    {a.strip().upper() for a in _allowed_env.split(",") if a.strip()} if _allowed_env else None
)

# Allowlist of application names permitted for OPEN_APPLICATION (env-configurable).
# If empty, no applications are allowed; set to "*" to allow any (not recommended in production).
_apps_env = os.getenv("ALLOWED_APP_NAMES", "")
ALLOWED_APP_NAMES: set[str] | None = (
    {a.strip() for a in _apps_env.split(",") if a.strip()} if _apps_env else None
)


DANGEROUS_KEY_COMBOS: set[str] = {
    "ctrl+alt+delete",
    "ctrl+alt+del",
    "alt+f4",
    "win+l",
    "super+l",
    "ctrl+shift+esc",
}

# PyAutoGUI safety: move to corner to abort
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
ACTIONS_TOTAL = Counter("actions_executed_total", "Total actions executed", ["action_type", "status"])
ACTION_LATENCY = Histogram("action_execution_latency_seconds", "Action execution latency", ["action_type"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ActionPayload(BaseModel):
    action_type: str
    coordinates: list[int] | None = None
    text: str | None = None
    key: str | None = None
    app_name: str | None = None
    url: str | None = None
    seconds: float | None = None
    # For DRAG_AND_DROP: second pair of coordinates
    end_coordinates: list[int] | None = None


class ExecuteRequest(BaseModel):
    task_id: str
    action: ActionPayload


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
async def get_db_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id          SERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload     JSONB NOT NULL DEFAULT '{}',
                success     BOOLEAN NOT NULL DEFAULT FALSE,
                message     TEXT,
                executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


async def log_action(
    pool: asyncpg.Pool,
    task_id: str,
    action_type: str,
    payload: dict[str, Any],
    success: bool,
    message: str,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO actions (task_id, action_type, payload, success, message, executed_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                task_id, action_type, json.dumps(payload), success, message, datetime.now(timezone.utc),
            )
    except Exception as exc:
        logger.warning("Failed to log action to DB: %s", exc)


# ---------------------------------------------------------------------------
# Sandboxing helpers
# ---------------------------------------------------------------------------
def validate_action_type(action_type: str) -> None:
    """Raise if this action type is not in the allowed set."""
    if ALLOWED_ACTIONS is not None and action_type.upper() not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=403,
            detail=f"Action '{action_type}' is not in ALLOWED_ACTIONS",
        )


def validate_coordinates(coords: list[int] | None) -> tuple[int, int] | None:
    """Return integer (x, y) or raise if out of screen bounds."""
    if not coords:
        return None
    if len(coords) < 2:
        raise HTTPException(status_code=400, detail="Coordinates must have at least 2 values [x, y]")
    x, y = int(coords[0]), int(coords[1])
    screen_w, screen_h = pyautogui.size()
    if not (0 <= x <= screen_w and 0 <= y <= screen_h):
        raise HTTPException(
            status_code=400,
            detail=f"Coordinates ({x}, {y}) are outside screen bounds ({screen_w}x{screen_h})",
        )
    return x, y


def validate_key(key: str | None) -> str:
    """Raise if the key is in the dangerous list and blocking is enabled."""
    if not key:
        raise HTTPException(status_code=400, detail="No key specified for PRESS_KEY action")
    if BLOCK_DANGEROUS_KEYS and key.lower() in DANGEROUS_KEY_COMBOS:
        raise HTTPException(status_code=403, detail=f"Key '{key}' is blocked by BLOCK_DANGEROUS_KEYS policy")
    return key


def validate_url(url: str | None) -> str:
    """Raise if URL is missing or uses a non-http scheme."""
    if not url:
        raise HTTPException(status_code=400, detail="No URL specified for OPEN_WEBSITE action")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"URL scheme not allowed: {url}")
    return url


# ---------------------------------------------------------------------------
# Action executor
# ---------------------------------------------------------------------------
async def execute_action(action: ActionPayload, task_id: str) -> str:
    """Execute the action and return a descriptive message. May raise on failure."""
    a = action.action_type.upper()
    validate_action_type(a)

    if SAFE_MODE:
        logger.info("[SAFE_MODE] Would execute %s payload=%s", a, action.model_dump())
        return f"[SAFE_MODE] Action {a} logged but not executed"

    if a == "MOVE_MOUSE":
        xy = validate_coordinates(action.coordinates)
        if not xy:
            raise HTTPException(status_code=400, detail="MOVE_MOUSE requires coordinates")
        pyautogui.moveTo(xy[0], xy[1], duration=0.2)
        return f"Mouse moved to ({xy[0]}, {xy[1]})"

    elif a == "CLICK":
        xy = validate_coordinates(action.coordinates)
        if xy:
            pyautogui.click(xy[0], xy[1])
        else:
            pyautogui.click()
        return f"Clicked at {xy}"

    elif a == "DOUBLE_CLICK":
        xy = validate_coordinates(action.coordinates)
        if xy:
            pyautogui.doubleClick(xy[0], xy[1])
        else:
            pyautogui.doubleClick()
        return f"Double-clicked at {xy}"

    elif a == "RIGHT_CLICK":
        xy = validate_coordinates(action.coordinates)
        if xy:
            pyautogui.rightClick(xy[0], xy[1])
        else:
            pyautogui.rightClick()
        return f"Right-clicked at {xy}"

    elif a == "TYPE_TEXT":
        if not action.text:
            raise HTTPException(status_code=400, detail="TYPE_TEXT requires 'text' field")
        pyautogui.write(action.text, interval=0.05)
        return f"Typed {len(action.text)} characters"

    elif a == "PRESS_KEY":
        key = validate_key(action.key)
        pyautogui.press(key)
        return f"Pressed key: {key}"

    elif a == "SCROLL_UP":
        xy = validate_coordinates(action.coordinates)
        if xy:
            pyautogui.scroll(3, x=xy[0], y=xy[1])
        else:
            pyautogui.scroll(3)
        return "Scrolled up"

    elif a == "SCROLL_DOWN":
        xy = validate_coordinates(action.coordinates)
        if xy:
            pyautogui.scroll(-3, x=xy[0], y=xy[1])
        else:
            pyautogui.scroll(-3)
        return "Scrolled down"

    elif a == "DRAG_AND_DROP":
        xy = validate_coordinates(action.coordinates)
        if not xy:
            raise HTTPException(status_code=400, detail="DRAG_AND_DROP requires start coordinates")
        end_xy = validate_coordinates(action.end_coordinates)
        if not end_xy:
            raise HTTPException(status_code=400, detail="DRAG_AND_DROP requires end_coordinates")
        pyautogui.moveTo(xy[0], xy[1], duration=0.2)
        pyautogui.drag(end_xy[0] - xy[0], end_xy[1] - xy[1], duration=0.5, button="left")
        return f"Dragged from {xy} to {end_xy}"

    elif a == "OPEN_APPLICATION":
        if not action.app_name:
            raise HTTPException(status_code=400, detail="OPEN_APPLICATION requires 'app_name' field")
        # Validate against the allowlist to prevent command injection
        if ALLOWED_APP_NAMES is None:
            raise HTTPException(
                status_code=403,
                detail="OPEN_APPLICATION is disabled. Set ALLOWED_APP_NAMES env var to enable it.",
            )
        if action.app_name not in ALLOWED_APP_NAMES:
            raise HTTPException(
                status_code=403,
                detail=f"Application '{action.app_name}' is not in ALLOWED_APP_NAMES",
            )
        # Pass as a list to avoid shell interpretation; no shell=True
        proc = subprocess.Popen(
            [action.app_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        return f"Launched application '{action.app_name}' (pid={proc.pid})"

    elif a == "OPEN_WEBSITE":
        url = validate_url(action.url)
        webbrowser.open(url)
        return f"Opened URL: {url}"

    elif a == "WAIT":
        seconds = max(0.0, min(float(action.seconds or 1.0), 60.0))
        await asyncio.sleep(seconds)
        return f"Waited {seconds}s"

    elif a == "TAKE_SCREENSHOT":
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{SCREEN_CAPTURE_URL}/capture")
                resp.raise_for_status()
                data = resp.json()
            return f"Screenshot taken: {data.get('image_path', 'unknown')}"
        except Exception as exc:
            logger.warning("Screen-capture service unavailable: %s — falling back to local pyautogui", exc)
            screenshot = pyautogui.screenshot()
            path = f"screenshot_{task_id}_{int(datetime.now(timezone.utc).timestamp())}.png"
            screenshot.save(path)
            return f"Screenshot saved locally: {path}"

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action_type: {a}")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
app = FastAPI(title="Action Execution Service", version="1.0.0")
db_pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup():
    global db_pool
    try:
        db_pool = await get_db_pool()
        await ensure_schema(db_pool)
        logger.info("PostgreSQL connected")
    except Exception as exc:
        logger.warning("PostgreSQL unavailable at startup: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    screen_w, screen_h = pyautogui.size()
    return {
        "status": "healthy",
        "safe_mode": SAFE_MODE,
        "block_dangerous_keys": BLOCK_DANGEROUS_KEYS,
        "allowed_actions": list(ALLOWED_ACTIONS) if ALLOWED_ACTIONS else "all",
        "screen_size": {"width": screen_w, "height": screen_h},
        "database": db_pool is not None,
    }


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/execute")
async def execute(req: ExecuteRequest):
    action_type = req.action.action_type.upper()
    logger.info("Execute action=%s task_id=%s safe_mode=%s", action_type, req.task_id, SAFE_MODE)

    start = time.perf_counter()
    success = False
    message = ""
    try:
        message = await execute_action(req.action, req.task_id)
        success = True
        ACTIONS_TOTAL.labels(action_type=action_type, status="success").inc()
    except HTTPException:
        ACTIONS_TOTAL.labels(action_type=action_type, status="rejected").inc()
        raise
    except Exception as exc:
        internal_message = f"Execution error: {exc}"
        # Log full details server-side; expose only a generic message to callers
        logger.error("Action %s failed for task_id=%s: %s", action_type, req.task_id, exc)
        message = internal_message
        ACTIONS_TOTAL.labels(action_type=action_type, status="error").inc()
    finally:
        elapsed = time.perf_counter() - start
        ACTION_LATENCY.labels(action_type=action_type).observe(elapsed)

    # Log to DB (fire-and-forget on failure)
    if db_pool:
        await log_action(
            db_pool,
            req.task_id,
            action_type,
            req.action.model_dump(exclude_none=True),
            success,
            message,
        )

    if not success:
        raise HTTPException(status_code=500, detail="Action execution failed. See server logs for details.")

    return {
        "success": success,
        "action_type": action_type,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/actions/{task_id}")
async def get_actions(task_id: str):
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, action_type, payload, success, message, executed_at FROM actions WHERE task_id = $1 ORDER BY executed_at",
            task_id,
        )
    return [
        {
            "id": r["id"],
            "task_id": task_id,
            "action_type": r["action_type"],
            "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
            "success": r["success"],
            "message": r["message"],
            "executed_at": r["executed_at"].isoformat(),
        }
        for r in rows
    ]
