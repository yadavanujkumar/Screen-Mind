import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from openai import AsyncOpenAI
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("llm-reasoning")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
LLM_LATENCY = Histogram(
    "llm_call_latency_seconds",
    "LLM call latency in seconds",
    ["provider", "model"],
)
LLM_TOKENS_USED = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed",
    ["provider", "model", "token_type"],
)
LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "Total LLM API calls",
    ["provider", "model", "status"],
)
LLM_ACTIVE_CALLS = Gauge(
    "llm_active_calls",
    "Currently in-flight LLM calls",
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ScreenState(BaseModel):
    screen_type: str = ""
    state_summary: str = ""
    key_text: list[str] = Field(default_factory=list)
    interactive_elements: list[Any] = Field(default_factory=list)
    ocr_text: str = ""


class ReasonRequest(BaseModel):
    task_id: str
    goal: str
    screen_state: ScreenState = Field(default_factory=ScreenState)
    step_number: int = 1
    memory_context: list[Any] = Field(default_factory=list)
    previous_actions: list[Any] = Field(default_factory=list)


class ExplainRequest(BaseModel):
    task_id: str
    decision: str
    context: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an AI computer control agent. Given the current screen state and goal, "
    "decide the best next action. Always respond with valid JSON only — no markdown fences."
)

RESPONSE_SCHEMA = """{
  "decision": "action description",
  "reason": "why this action",
  "alternatives": ["alternative 1", "alternative 2"],
  "confidence": 0.0,
  "next_action": {
    "action_type": "CLICK|TYPE_TEXT|PRESS_KEY|SCROLL_UP|SCROLL_DOWN|MOVE_MOUSE|DOUBLE_CLICK|RIGHT_CLICK|DRAG_AND_DROP|OPEN_APPLICATION|OPEN_WEBSITE|WAIT|TAKE_SCREENSHOT",
    "coordinates": [0, 0],
    "text": "text to type",
    "key": "key name",
    "app_name": "app",
    "url": "url",
    "seconds": 1.0
  },
  "expected_outcome": "what should happen after this action",
  "task_complete": false
}"""


def build_user_prompt(req: ReasonRequest) -> str:
    ss = req.screen_state
    return (
        f"Goal: {req.goal}\n"
        f"Current Screen: {ss.screen_type}\n"
        f"Screen Summary: {ss.state_summary}\n"
        f"Key Text Visible: {json.dumps(ss.key_text)}\n"
        f"Interactive Elements: {json.dumps(ss.interactive_elements)}\n"
        f"Previous Actions: {json.dumps(req.previous_actions)}\n"
        f"Memory Context: {json.dumps(req.memory_context)}\n"
        f"Step Number: {req.step_number}\n\n"
        f"Respond in JSON format:\n{RESPONSE_SCHEMA}"
    )


def parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse and validate JSON from the LLM response."""
    raw = raw.strip()
    # Strip optional markdown code fences the model may add
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw)


def mock_response(req: ReasonRequest) -> dict[str, Any]:
    logger.warning("Returning mock LLM response (no provider configured)")
    return {
        "decision": f"Take screenshot to assess step {req.step_number}",
        "reason": "No LLM provider configured — using mock response for testing.",
        "alternatives": ["Wait and retry", "Ask user for clarification"],
        "confidence": 0.5,
        "next_action": {"action_type": "TAKE_SCREENSHOT"},
        "expected_outcome": "A fresh screenshot will reveal the current state.",
        "task_complete": False,
        "model_used": "mock",
        "latency_ms": 0.0,
    }


# ---------------------------------------------------------------------------
# LLM callers
# ---------------------------------------------------------------------------
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def call_openai(messages: list[dict]) -> tuple[str, str, int, int]:
    """Call OpenAI. Returns (content, model, prompt_tokens, completion_tokens)."""
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    choice = response.choices[0]
    usage = response.usage
    return (
        choice.message.content or "",
        response.model,
        usage.prompt_tokens if usage else 0,
        usage.completion_tokens if usage else 0,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def call_ollama(messages: list[dict]) -> tuple[str, str, int, int]:
    """Call Ollama chat endpoint. Returns (content, model, prompt_tokens, completion_tokens)."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("message", {}).get("content", "")
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    return content, OLLAMA_MODEL, prompt_tokens, completion_tokens


async def call_llm(messages: list[dict]) -> tuple[str, str, int, int]:
    """Route to the configured provider."""
    if LLM_PROVIDER == "ollama":
        return await call_ollama(messages)
    if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
        return await call_openai(messages)
    raise ValueError("No LLM provider configured")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="LLM Reasoning Service", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "healthy", "provider": LLM_PROVIDER, "model": OPENAI_MODEL if LLM_PROVIDER == "openai" else OLLAMA_MODEL}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/reason")
async def reason(req: ReasonRequest):
    logger.info("Reason request task_id=%s step=%d", req.task_id, req.step_number)

    # Check if any provider is available
    provider_available = (LLM_PROVIDER == "openai" and bool(OPENAI_API_KEY)) or LLM_PROVIDER == "ollama"
    if not provider_available:
        return mock_response(req)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(req)},
    ]

    LLM_ACTIVE_CALLS.inc()
    start = time.perf_counter()
    status = "success"
    model_used = OPENAI_MODEL if LLM_PROVIDER == "openai" else OLLAMA_MODEL

    try:
        raw_content, model_used, prompt_tokens, completion_tokens = await call_llm(messages)
        parsed = parse_llm_response(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse error: %s", exc)
        status = "parse_error"
        raise HTTPException(status_code=502, detail=f"LLM returned invalid JSON: {exc}")
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        status = "error"
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    finally:
        elapsed = time.perf_counter() - start
        LLM_ACTIVE_CALLS.dec()
        LLM_LATENCY.labels(provider=LLM_PROVIDER, model=model_used).observe(elapsed)
        LLM_CALLS_TOTAL.labels(provider=LLM_PROVIDER, model=model_used, status=status).inc()

    if status == "success":
        LLM_TOKENS_USED.labels(provider=LLM_PROVIDER, model=model_used, token_type="prompt").inc(prompt_tokens)
        LLM_TOKENS_USED.labels(provider=LLM_PROVIDER, model=model_used, token_type="completion").inc(completion_tokens)

    return {
        "decision": parsed.get("decision", ""),
        "reason": parsed.get("reason", ""),
        "alternatives": parsed.get("alternatives", []),
        "confidence": float(parsed.get("confidence", 0.0)),
        "next_action": parsed.get("next_action", {}),
        "expected_outcome": parsed.get("expected_outcome", ""),
        "task_complete": bool(parsed.get("task_complete", False)),
        "model_used": model_used,
        "latency_ms": round(elapsed * 1000, 2),
    }


@app.post("/explain")
async def explain(req: ExplainRequest):
    logger.info("Explain request task_id=%s", req.task_id)

    provider_available = (LLM_PROVIDER == "openai" and bool(OPENAI_API_KEY)) or LLM_PROVIDER == "ollama"
    if not provider_available:
        return {"explanation": "No LLM provider configured.", "task_id": req.task_id}

    user_prompt = (
        f"Explain in plain English why the following decision was made by an AI computer control agent.\n\n"
        f"Decision: {req.decision}\n"
        f"Context: {json.dumps(req.context, indent=2)}\n\n"
        "Provide a concise, human-readable explanation."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    LLM_ACTIVE_CALLS.inc()
    start = time.perf_counter()
    try:
        raw_content, model_used, _, _ = await call_llm(messages)
    except Exception as exc:
        logger.error("Explain LLM call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    finally:
        elapsed = time.perf_counter() - start
        LLM_ACTIVE_CALLS.dec()

    return {
        "task_id": req.task_id,
        "explanation": raw_content.strip(),
        "model_used": model_used,
        "latency_ms": round(elapsed * 1000, 2),
    }
