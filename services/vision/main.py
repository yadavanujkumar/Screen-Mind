import base64
import logging
import os
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from PIL import Image, ImageFilter
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vision")

app = FastAPI(title="Screen-Mind Vision Service", version="1.0.0")

ANALYSES_TOTAL = Counter("vision_analyses_total", "Total analyses performed")
ANALYSIS_ERRORS = Counter("vision_analysis_errors_total", "Total analysis errors")
ANALYSIS_DURATION = Histogram("vision_analysis_duration_seconds", "Analysis duration")

# ---------------------------------------------------------------------------
# Lazy OCR reader initialisation
# ---------------------------------------------------------------------------
_ocr_reader = None
_USE_MOCK_OCR = os.environ.get("MOCK_OCR", "").lower() in ("1", "true", "yes")


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    if _USE_MOCK_OCR:
        logger.info("MOCK_OCR enabled – skipping easyocr initialisation")
        return None
    try:
        import easyocr  # noqa: PLC0415
        logger.info("Initialising easyocr Reader (first call – may be slow)…")
        _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        logger.info("easyocr Reader ready")
    except Exception as exc:
        logger.error("Failed to initialise easyocr: %s", exc)
        raise
    return _ocr_reader


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    screenshot_b64: str


class DetectedElement(BaseModel):
    type: str
    text: str
    bbox: list[int]  # [x1, y1, x2, y2]
    confidence: float


class AnalyzeResponse(BaseModel):
    ocr_text: str
    detected_elements: list[DetectedElement]
    raw_ocr_results: list[Any]
    timestamp: str


class OcrOnlyResponse(BaseModel):
    ocr_text: str
    raw_ocr_results: list[Any]
    timestamp: str


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _b64_to_image(b64: str) -> Image.Image:
    try:
        data = base64.b64decode(b64)
        return Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"Invalid base64 image data: {exc}") from exc


def _run_ocr(img: Image.Image) -> tuple[str, list[Any]]:
    """Return (joined text, raw results list)."""
    if _USE_MOCK_OCR:
        return "mock ocr text", []

    reader = _get_ocr_reader()
    img_array = np.array(img)
    raw = reader.readtext(img_array, detail=1)
    # raw items: (bbox_points, text, confidence)
    text_parts = [item[1] for item in raw if item[2] > 0.1]
    return " ".join(text_parts), raw


def _bbox_points_to_rect(bbox_points) -> list[int]:
    """Convert easyocr [[x,y],...] bbox to [x1,y1,x2,y2]."""
    xs = [p[0] for p in bbox_points]
    ys = [p[1] for p in bbox_points]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _classify_text_element(text: str) -> str:
    """Heuristically classify an OCR text region as a UI element type."""
    lower = text.lower().strip()
    button_keywords = {
        "ok", "cancel", "submit", "save", "delete", "close", "open", "yes",
        "no", "next", "back", "continue", "login", "sign in", "sign up",
        "register", "search", "apply", "confirm", "done", "exit", "help",
        "retry", "refresh", "update", "edit", "add", "remove",
    }
    if lower.rstrip(".!?") in button_keywords:
        return "button"
    if any(ch in text for ch in (":", "…")) and len(text) < 50:
        return "label"
    if lower.startswith(("enter ", "type ", "search ", "e.g.")):
        return "input"
    return "text"


def _detect_rect_elements(img: Image.Image, raw_ocr: list[Any]) -> list[DetectedElement]:
    """
    Detect UI elements:
    1. Classify OCR-found text regions.
    2. Apply simple edge / contour detection to find rectangular shapes
       (potential buttons / input boxes) that contain no OCR text.
    """
    elements: list[DetectedElement] = []

    # 1. OCR-derived elements
    for item in raw_ocr:
        bbox_points, text, confidence = item
        if confidence < 0.1 or not text.strip():
            continue
        rect = _bbox_points_to_rect(bbox_points)
        elem_type = _classify_text_element(text)
        elements.append(
            DetectedElement(
                type=elem_type,
                text=text.strip(),
                bbox=rect,
                confidence=round(float(confidence), 4),
            )
        )

    # 2. Rectangle detection via PIL edge processing
    try:
        gray = img.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edges_np = np.array(edges)
        threshold = 30
        binary = (edges_np > threshold).astype(np.uint8)

        # Scan for horizontal runs that could be button/input borders
        h, w = binary.shape
        min_width, min_height = 30, 10
        max_width, max_height = w // 2, h // 4

        visited_rows: set[int] = set()
        for row in range(0, h - min_height, 4):
            if row in visited_rows:
                continue
            row_data = binary[row]
            # Find contiguous edge runs
            col = 0
            while col < w - min_width:
                if row_data[col]:
                    start_col = col
                    while col < w and row_data[col]:
                        col += 1
                    run_len = col - start_col
                    if min_width <= run_len <= max_width:
                        # Look for a matching bottom edge within min_height..max_height
                        for bot_row in range(row + min_height, min(row + max_height, h)):
                            bot_run = binary[bot_row, start_col : start_col + run_len]
                            if bot_run.mean() > 0.5:
                                rect = [start_col, row, start_col + run_len, bot_row]
                                # Skip if already covered by an OCR element
                                covered = any(
                                    e.bbox[0] <= rect[0] and e.bbox[1] <= rect[1]
                                    and e.bbox[2] >= rect[2] and e.bbox[3] >= rect[3]
                                    for e in elements
                                )
                                if not covered:
                                    height_px = bot_row - row
                                    e_type = "input" if height_px < 35 else "button"
                                    elements.append(
                                        DetectedElement(
                                            type=e_type,
                                            text="",
                                            bbox=rect,
                                            confidence=0.5,
                                        )
                                    )
                                visited_rows.update(range(row, bot_row + 1))
                                break
                else:
                    col += 1
    except Exception as exc:
        logger.warning("Rectangle detection failed (non-fatal): %s", exc)

    return elements


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    start = time.monotonic()
    try:
        img = _b64_to_image(request.screenshot_b64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        ocr_text, raw_ocr = _run_ocr(img)
        detected = _detect_rect_elements(img, raw_ocr)

        # Serialise raw_ocr to JSON-safe format
        serialisable_raw: list[Any] = []
        for item in raw_ocr:
            bbox_points, text, confidence = item
            serialisable_raw.append(
                {
                    "bbox": [[int(x), int(y)] for x, y in bbox_points],
                    "text": text,
                    "confidence": round(float(confidence), 4),
                }
            )

        ANALYSES_TOTAL.inc()
        return AnalyzeResponse(
            ocr_text=ocr_text,
            detected_elements=detected,
            raw_ocr_results=serialisable_raw,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        ANALYSIS_ERRORS.inc()
        logger.exception("Analysis failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc
    finally:
        ANALYSIS_DURATION.observe(time.monotonic() - start)


@app.post("/analyze/ocr-only", response_model=OcrOnlyResponse)
async def analyze_ocr_only(request: AnalyzeRequest) -> OcrOnlyResponse:
    start = time.monotonic()
    try:
        img = _b64_to_image(request.screenshot_b64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        ocr_text, raw_ocr = _run_ocr(img)

        serialisable_raw: list[Any] = []
        for item in raw_ocr:
            bbox_points, text, confidence = item
            serialisable_raw.append(
                {
                    "bbox": [[int(x), int(y)] for x, y in bbox_points],
                    "text": text,
                    "confidence": round(float(confidence), 4),
                }
            )

        ANALYSES_TOTAL.inc()
        return OcrOnlyResponse(
            ocr_text=ocr_text,
            raw_ocr_results=serialisable_raw,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        ANALYSIS_ERRORS.inc()
        logger.exception("OCR-only analysis failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"OCR failed: {exc}") from exc
    finally:
        ANALYSIS_DURATION.observe(time.monotonic() - start)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "vision",
        "mock_ocr": _USE_MOCK_OCR,
        "ocr_ready": _ocr_reader is not None or _USE_MOCK_OCR,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
