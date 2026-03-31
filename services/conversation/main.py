"""
Conversation Service
Enables conversational interaction with the Screen-Mind agent.
Users can ask questions, give remote directions, and have the agent
execute tasks on their behalf through natural language.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("conversation")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LLM_REASONING_URL = os.getenv("LLM_REASONING_URL", "http://llm-reasoning:8005")
TASK_PLANNER_URL = os.getenv("TASK_PLANNER_URL", "http://task-planner:8006")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "50"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
MESSAGES_TOTAL = Counter("conversation_messages_total", "Total conversation messages", ["intent"])
SESSIONS_CREATED = Counter("conversation_sessions_created_total", "Conversation sessions created")
RESPONSE_LATENCY = Histogram(
    "conversation_response_latency_seconds",
    "Latency of conversation LLM responses",
)
TASKS_TRIGGERED = Counter("conversation_tasks_triggered_total", "Tasks triggered via conversation")

# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------

# session_id -> session dict
_sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class NewSessionRequest(BaseModel):
    user_id: str
    title: Optional[str] = None


class MessageRequest(BaseModel):
    role: str = Field(default="user", pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ExecuteDirectionRequest(BaseModel):
    message_index: Optional[int] = None  # which direction message to execute; defaults to latest


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CONVERSATION_SYSTEM_PROMPT = (
    "You are Screen-Mind, an AI computer control agent assistant. "
    "You can answer questions about your capabilities and engage in helpful conversation. "
    "When the user gives you a direction or instruction to do something on their computer "
    "(e.g. 'open Chrome', 'search for X', 'close the current window', 'take a screenshot'), "
    "you MUST set intent to 'direction' and provide a clear task_description. "
    "For general questions or chitchat set intent to 'question'. "
    "For requests that need more information set intent to 'clarification'. "
    "Always respond with valid JSON only — no markdown fences.\n\n"
    "Response schema:\n"
    "{\n"
    '  "reply": "Your human-readable conversational reply",\n'
    '  "intent": "direction|question|clarification",\n'
    '  "task_description": "Precise task description if intent is direction, else null",\n'
    '  "requires_execution": true|false\n'
    "}"
)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


def _build_messages(history: list[dict[str, str]], new_content: str) -> list[dict[str, str]]:
    """Build the messages list for the LLM call, including session history."""
    messages: list[dict[str, str]] = [{"role": "system", "content": CONVERSATION_SYSTEM_PROMPT}]
    # Include recent history (up to MAX_HISTORY_MESSAGES turns)
    recent = history[-MAX_HISTORY_MESSAGES:]
    messages.extend({"role": m["role"], "content": m["content"]} for m in recent)
    messages.append({"role": "user", "content": new_content})
    return messages


async def _call_openai(messages: list[dict]) -> str:
    from openai import AsyncOpenAI  # type: ignore[import]

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


async def _call_ollama(messages: list[dict]) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")


DIRECTION_KEYWORDS = [
    "open", "close", "click", "type", "navigate", "search", "go to", "launch",
    "start", "stop", "run", "execute", "press", "scroll", "drag", "download",
    "upload", "copy", "paste", "delete", "move", "resize", "take screenshot",
]


def _mock_llm_response(content: str) -> dict[str, Any]:
    """Return a mock response when no LLM provider is configured."""
    lower = content.lower()
    is_direction = any(word in lower for word in DIRECTION_KEYWORDS)
    if is_direction:
        return {
            "reply": f"I'll execute that for you: {content}",
            "intent": "direction",
            "task_description": content,
            "requires_execution": True,
        }
    return {
        "reply": (
            "I'm Screen-Mind, your AI computer control agent. "
            "I can execute tasks on your screen — just tell me what to do! "
            "(Note: no LLM provider is configured, so I'm running in mock mode.)"
        ),
        "intent": "question",
        "task_description": None,
        "requires_execution": False,
    }


async def _call_llm(messages: list[dict]) -> dict[str, Any]:
    """Route to configured LLM provider and return parsed response dict."""
    provider_available = (LLM_PROVIDER == "openai" and bool(OPENAI_API_KEY)) or LLM_PROVIDER == "ollama"
    if not provider_available:
        # Use last user message content for mock heuristic
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return _mock_llm_response(user_msg)

    if LLM_PROVIDER == "ollama":
        raw = await _call_ollama(messages)
    else:
        raw = await _call_openai(messages)

    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Task creation helper
# ---------------------------------------------------------------------------


async def _create_task(task_description: str, user_id: str) -> dict[str, Any]:
    """Forward a direction to the task-planner service as a new task."""
    payload = {"task_description": task_description, "user_id": user_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TASK_PLANNER_URL}/tasks", json=payload)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Conversation Service", version="1.0.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "conversation",
        "active_sessions": len(_sessions),
        "llm_provider": LLM_PROVIDER,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@app.post("/sessions", status_code=201)
async def create_session(req: NewSessionRequest) -> dict[str, Any]:
    """Create a new conversation session."""
    session_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()
    session: dict[str, Any] = {
        "session_id": session_id,
        "user_id": req.user_id,
        "title": req.title or f"Session {session_id[:8]}",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "task_ids": [],
    }
    _sessions[session_id] = session
    SESSIONS_CREATED.inc()
    logger.info("Created session %s for user %s", session_id, req.user_id)
    return {
        "session_id": session_id,
        "user_id": req.user_id,
        "title": session["title"],
        "created_at": now,
        "message_count": 0,
    }


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """Retrieve conversation session info and full message history."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    session = _sessions[session_id]
    return {
        "session_id": session_id,
        "user_id": session["user_id"],
        "title": session["title"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "messages": session["messages"],
        "task_ids": session["task_ids"],
        "message_count": len(session["messages"]),
    }


@app.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: MessageRequest) -> dict[str, Any]:
    """
    Send a user message to the conversation.
    The service determines whether the message is a direction (to execute)
    or a question/clarification, and responds accordingly.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    now = datetime.now(tz=timezone.utc).isoformat()

    # Add user message to history
    user_entry: dict[str, Any] = {
        "role": "user",
        "content": req.content,
        "timestamp": now,
        "intent": None,
        "task_id": None,
    }
    session["messages"].append(user_entry)

    # Build messages for LLM
    llm_messages = _build_messages(
        [m for m in session["messages"][:-1]],  # history excluding the just-added message
        req.content,
    )

    start = time.perf_counter()
    try:
        parsed = await _call_llm(llm_messages)
    except json.JSONDecodeError as exc:
        logger.error("LLM JSON parse error: %s", exc)
        raise HTTPException(status_code=502, detail="LLM returned invalid JSON")
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    finally:
        elapsed = time.perf_counter() - start
        RESPONSE_LATENCY.observe(elapsed)

    intent: str = parsed.get("intent", "question")
    reply: str = parsed.get("reply", "")
    task_description: Optional[str] = parsed.get("task_description")

    # Update user message intent
    user_entry["intent"] = intent
    MESSAGES_TOTAL.labels(intent=intent).inc()

    # Add assistant reply to history
    assistant_entry: dict[str, Any] = {
        "role": "assistant",
        "content": reply,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "intent": intent,
        "task_id": None,
    }
    session["messages"].append(assistant_entry)
    session["updated_at"] = datetime.now(tz=timezone.utc).isoformat()

    logger.info("session=%s intent=%s latency=%.3fs", session_id, intent, elapsed)

    return {
        "session_id": session_id,
        "reply": reply,
        "intent": intent,
        "task_description": task_description,
        "requires_execution": parsed.get("requires_execution", False),
        "message_index": len(session["messages"]) - 1,
        "latency_ms": round(elapsed * 1000, 2),
    }


@app.post("/sessions/{session_id}/execute")
async def execute_direction(session_id: str, req: ExecuteDirectionRequest) -> dict[str, Any]:
    """
    Execute a direction from the conversation as a task.
    Looks up the direction message at `message_index` (or latest direction) and
    forwards it to the task-planner service.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sessions[session_id]
    messages = session["messages"]

    # Find the target direction message
    if req.message_index is not None:
        if req.message_index < 0 or req.message_index >= len(messages):
            raise HTTPException(status_code=400, detail="Invalid message_index")
        target = messages[req.message_index]
    else:
        # Find the latest direction from user messages
        target = next(
            (m for m in reversed(messages) if m["role"] == "user" and m.get("intent") == "direction"),
            None,
        )
        if target is None:
            # Fall back to the latest user message
            target = next((m for m in reversed(messages) if m["role"] == "user"), None)
        if target is None:
            raise HTTPException(status_code=400, detail="No direction found in conversation")

    task_description = target["content"]

    try:
        task = await _create_task(task_description, session["user_id"])
    except httpx.HTTPError as exc:
        logger.error("Task creation failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Task planner unavailable: {exc}")

    task_id = task.get("task_id", "")
    target["task_id"] = task_id
    session["task_ids"].append(task_id)
    session["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    TASKS_TRIGGERED.inc()

    logger.info("Triggered task %s from session %s", task_id, session_id)

    return {
        "session_id": session_id,
        "task_id": task_id,
        "task_description": task_description,
        "task": task,
    }
