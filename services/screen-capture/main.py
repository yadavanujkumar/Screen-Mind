import base64
import logging
import os
import time
from datetime import datetime, timezone
from io import BytesIO

import mss
import mss.tools
from fastapi import FastAPI, HTTPException, Query
from PIL import Image
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("screen-capture")

os.environ.setdefault("DISPLAY", ":0")

app = FastAPI(title="Screen-Mind Screen Capture Service", version="1.0.0")

CAPTURES_TOTAL = Counter("screen_captures_total", "Total number of screen captures")
CAPTURE_ERRORS = Counter("screen_capture_errors_total", "Total screen capture errors")
CAPTURE_DURATION = Histogram("screen_capture_duration_seconds", "Screen capture duration")

RATE_LIMIT_INTERVAL = 1.0 / 10  # 10 captures/second max
_last_capture_time: float = 0.0
_last_screenshot: dict | None = None


class CaptureResponse(BaseModel):
    screenshot_b64: str
    width: int
    height: int
    timestamp: str


def _enforce_rate_limit() -> None:
    global _last_capture_time
    now = time.monotonic()
    elapsed = now - _last_capture_time
    if elapsed < RATE_LIMIT_INTERVAL:
        time.sleep(RATE_LIMIT_INTERVAL - elapsed)
    _last_capture_time = time.monotonic()


def _image_to_b64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _capture_full() -> CaptureResponse:
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        screenshot = sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    return CaptureResponse(
        screenshot_b64=_image_to_b64(img),
        width=img.width,
        height=img.height,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _capture_region(x: int, y: int, width: int, height: int) -> CaptureResponse:
    region = {"top": y, "left": x, "width": width, "height": height}
    with mss.mss() as sct:
        screenshot = sct.grab(region)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    return CaptureResponse(
        screenshot_b64=_image_to_b64(img),
        width=img.width,
        height=img.height,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/capture", response_model=CaptureResponse)
async def capture_screen() -> CaptureResponse:
    global _last_screenshot
    _enforce_rate_limit()
    start = time.monotonic()
    try:
        result = _capture_full()
        _last_screenshot = result.model_dump()
        CAPTURES_TOTAL.inc()
        return result
    except Exception as exc:
        CAPTURE_ERRORS.inc()
        logger.exception("Failed to capture screen: %s", exc)
        raise HTTPException(status_code=500, detail=f"Screen capture failed: {exc}") from exc
    finally:
        CAPTURE_DURATION.observe(time.monotonic() - start)


@app.get("/capture/region", response_model=CaptureResponse)
async def capture_region(
    x: int = Query(0, ge=0, description="Left offset in pixels"),
    y: int = Query(0, ge=0, description="Top offset in pixels"),
    width: int = Query(100, ge=1, description="Region width in pixels"),
    height: int = Query(100, ge=1, description="Region height in pixels"),
) -> CaptureResponse:
    global _last_screenshot
    _enforce_rate_limit()
    start = time.monotonic()
    try:
        result = _capture_region(x, y, width, height)
        _last_screenshot = result.model_dump()
        CAPTURES_TOTAL.inc()
        return result
    except Exception as exc:
        CAPTURE_ERRORS.inc()
        logger.exception("Failed to capture region (%s,%s %sx%s): %s", x, y, width, height, exc)
        raise HTTPException(status_code=500, detail=f"Region capture failed: {exc}") from exc
    finally:
        CAPTURE_DURATION.observe(time.monotonic() - start)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "screen-capture",
        "display": os.environ.get("DISPLAY", "not set"),
        "last_capture": _last_screenshot["timestamp"] if _last_screenshot else None,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
