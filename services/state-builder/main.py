import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("state-builder")

app = FastAPI(title="Screen-Mind State Builder Service", version="1.0.0")

BUILDS_TOTAL = Counter("state_builds_total", "Total state builds performed")
BUILD_ERRORS = Counter("state_build_errors_total", "Total state build errors")
BUILD_DURATION = Histogram("state_build_duration_seconds", "State build duration")

# ---------------------------------------------------------------------------
# Screen-type detection keyword map
# Each key maps to a list of keyword sets; any full match scores a point.
# ---------------------------------------------------------------------------
_SCREEN_TYPE_KEYWORDS: dict[str, list[str]] = {
    "login": ["login", "sign in", "password", "username", "email", "forgot password"],
    "registration": ["register", "sign up", "create account", "confirm password"],
    "dashboard": ["dashboard", "overview", "summary", "welcome", "analytics"],
    "form": ["submit", "required", "please fill", "form", "input", "select"],
    "error": ["error", "exception", "failed", "not found", "unauthorized", "forbidden", "500", "404"],
    "settings": ["settings", "preferences", "configuration", "options", "profile"],
    "file_dialog": ["open file", "save as", "browse", "directory", "folder", "file name"],
    "alert_dialog": ["alert", "warning", "are you sure", "confirm", "proceed", "cancel"],
    "browser": ["http://", "https://", "www.", ".com", "back", "forward", "reload", "address"],
    "terminal": ["$", "#", "bash", "shell", "command", "root@", "user@"],
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BuildRequest(BaseModel):
    screenshot_b64: str = ""  # not used directly but kept for pipeline consistency
    ocr_text: str = ""
    detected_elements: list[dict[str, Any]] = []
    task_id: str = ""
    goal: str = ""


class BuildResponse(BaseModel):
    screen_type: str
    key_text: list[str]
    interactive_elements: list[dict[str, Any]]
    state_summary: str
    timestamp: str


# ---------------------------------------------------------------------------
# State-building helpers
# ---------------------------------------------------------------------------

def _detect_screen_type(ocr_text: str, elements: list[dict[str, Any]]) -> str:
    """Return the best-matching screen type label."""
    lower_text = ocr_text.lower()
    scores: dict[str, int] = {k: 0 for k in _SCREEN_TYPE_KEYWORDS}

    for screen_type, keywords in _SCREEN_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in lower_text:
                scores[screen_type] += 1

    # Factor in element types
    element_types = [e.get("type", "") for e in elements]
    if element_types.count("input") >= 2:
        scores["form"] += 1
    if element_types.count("button") >= 1:
        scores["form"] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "unknown"
    return best


def _extract_key_text(ocr_text: str) -> list[str]:
    """
    Extract meaningful lines from OCR text:
    - Non-empty lines
    - Titles (capitalised short phrases)
    - Potential labels/error messages
    """
    if not ocr_text.strip():
        return []

    lines = [ln.strip() for ln in re.split(r"[\n\r]+|  +", ocr_text) if ln.strip()]

    key: list[str] = []
    seen: set[str] = set()
    error_pattern = re.compile(
        r"\b(error|warning|failed|invalid|required|not found|unauthorized)\b",
        re.IGNORECASE,
    )

    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        # Always keep error/warning lines
        if error_pattern.search(line):
            key.append(line)
            continue
        # Keep short capitalised lines (likely titles/headings)
        if len(line) < 80 and (line[0].isupper() or line[0].isdigit()):
            key.append(line)
            continue
        # Keep lines that look like labels (contain colon)
        if ":" in line and len(line) < 60:
            key.append(line)

    return key[:20]  # cap at 20 to avoid flooding


def _categorise_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return only interactive element types with normalised structure.
    Filters out pure text regions with no action potential.
    """
    interactive_types = {"button", "input", "checkbox", "radio", "dropdown", "link", "select"}
    result: list[dict[str, Any]] = []

    for elem in elements:
        etype = elem.get("type", "text")
        if etype not in interactive_types:
            # Promote label-adjacent short texts as potential interactive
            text = elem.get("text", "")
            if len(text) < 25 and text.strip():
                etype = "label"
            else:
                continue

        result.append(
            {
                "type": etype,
                "text": elem.get("text", ""),
                "bbox": elem.get("bbox", []),
                "confidence": elem.get("confidence", 0.0),
            }
        )

    return result


def _build_state_summary(
    screen_type: str,
    key_text: list[str],
    interactive_elements: list[dict[str, Any]],
    goal: str,
    task_id: str,
) -> str:
    """Compose a human-readable state summary."""
    parts: list[str] = []

    if task_id:
        parts.append(f"Task '{task_id}'")
    if goal:
        parts.append(f"targeting goal: {goal!r}")

    parts.append(f"Screen type detected: {screen_type}.")

    if key_text:
        headline = key_text[0]
        parts.append(f"Headline text: {headline!r}.")

    btn_count = sum(1 for e in interactive_elements if e["type"] == "button")
    inp_count = sum(1 for e in interactive_elements if e["type"] == "input")
    if btn_count:
        parts.append(f"{btn_count} button(s) visible.")
    if inp_count:
        parts.append(f"{inp_count} input field(s) visible.")

    errors = [t for t in key_text if re.search(r"\b(error|warning|failed)\b", t, re.IGNORECASE)]
    if errors:
        parts.append(f"Attention – errors/warnings detected: {errors[0]!r}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/build", response_model=BuildResponse)
async def build_state(request: BuildRequest) -> BuildResponse:
    start = time.monotonic()
    try:
        screen_type = _detect_screen_type(request.ocr_text, request.detected_elements)
        key_text = _extract_key_text(request.ocr_text)
        interactive_elements = _categorise_elements(request.detected_elements)
        state_summary = _build_state_summary(
            screen_type,
            key_text,
            interactive_elements,
            request.goal,
            request.task_id,
        )

        BUILDS_TOTAL.inc()
        logger.info(
            "Built state for task=%r: screen_type=%s elements=%d",
            request.task_id,
            screen_type,
            len(interactive_elements),
        )
        return BuildResponse(
            screen_type=screen_type,
            key_text=key_text,
            interactive_elements=interactive_elements,
            state_summary=state_summary,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        BUILD_ERRORS.inc()
        logger.exception("State build failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"State build failed: {exc}") from exc
    finally:
        BUILD_DURATION.observe(time.monotonic() - start)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "state-builder"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
