"""Verification Service - Compare before/after screenshots and OCR to verify action success."""
import base64
import io
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ERROR_PATTERNS = re.compile(
    r"\b(error|failed|failure|exception|denied|not found|invalid|unauthorized"
    r"|forbidden|timeout|refused|crashed|aborted|fatal|critical|warning)\b",
    re.IGNORECASE,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Verification service started")
    yield
    logger.info("Verification service shut down")


app = FastAPI(title="Verification Service", version="1.0.0", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    task_id: str
    expected_outcome: str
    screenshot_b64: str
    previous_screenshot_b64: str
    action_taken: dict
    ocr_text: str = ""


class VerifyResponse(BaseModel):
    verified: bool
    confidence: float
    changes_detected: bool
    error_detected: bool
    error_message: str
    verification_details: dict


# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_image(b64_str: str) -> Image.Image:
    """Decode a base64-encoded PNG/JPEG into a PIL Image."""
    # Strip data-URI prefix if present
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    raw = base64.b64decode(b64_str)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def pixel_difference(img_before: Image.Image, img_after: Image.Image) -> dict:
    """Compute pixel-level difference statistics between two images."""
    # Resize to the same dimensions if they differ
    if img_before.size != img_after.size:
        img_after = img_after.resize(img_before.size, Image.LANCZOS)

    arr_before = np.array(img_before, dtype=np.int32)
    arr_after = np.array(img_after, dtype=np.int32)

    diff = np.abs(arr_after - arr_before)
    total_pixels = arr_before.shape[0] * arr_before.shape[1]
    changed_pixels = int(np.sum(diff.max(axis=2) > 10))  # threshold: 10 intensity units
    mean_diff = float(diff.mean())
    max_diff = float(diff.max())
    changed_fraction = changed_pixels / total_pixels

    return {
        "total_pixels": total_pixels,
        "changed_pixels": changed_pixels,
        "changed_fraction": round(changed_fraction, 4),
        "mean_pixel_diff": round(mean_diff, 2),
        "max_pixel_diff": round(max_diff, 2),
    }


def check_expected_keywords(expected_outcome: str, ocr_text: str) -> dict:
    """Check how many keywords from expected_outcome appear in OCR text."""
    # Extract meaningful words (>3 chars) from expected outcome
    keywords = [w.lower() for w in re.findall(r"\b\w{3,}\b", expected_outcome)]
    if not keywords:
        return {"matched": 0, "total": 0, "ratio": 0.0, "matched_keywords": []}

    ocr_lower = ocr_text.lower()
    matched = [kw for kw in keywords if kw in ocr_lower]
    ratio = len(matched) / len(keywords)
    return {
        "matched": len(matched),
        "total": len(keywords),
        "ratio": round(ratio, 4),
        "matched_keywords": matched,
    }


def check_errors(ocr_text: str) -> dict:
    """Detect error indicators in OCR text."""
    matches = ERROR_PATTERNS.findall(ocr_text)
    unique_matches = list({m.lower() for m in matches})
    return {
        "error_detected": len(matches) > 0,
        "error_terms": unique_matches,
    }


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/verify", response_model=VerifyResponse)
async def verify_action(req: VerifyRequest):
    details: dict = {}
    error_message = ""
    confidence_components: list[float] = []

    # 1. Screenshot pixel difference
    try:
        img_before = decode_image(req.previous_screenshot_b64)
        img_after = decode_image(req.screenshot_b64)
        diff_stats = pixel_difference(img_before, img_after)
        details["pixel_diff"] = diff_stats
        changes_detected = diff_stats["changed_fraction"] > 0.005  # >0.5% pixels changed
    except Exception as exc:
        logger.warning("Screenshot comparison failed: %s", exc)
        details["pixel_diff"] = {"error": str(exc)}
        changes_detected = False

    # 2. Keyword matching against OCR
    keyword_result = check_expected_keywords(req.expected_outcome, req.ocr_text)
    details["keyword_match"] = keyword_result
    keyword_confidence = keyword_result["ratio"]
    confidence_components.append(keyword_confidence)

    # 3. Error detection in OCR
    error_result = check_errors(req.ocr_text)
    details["error_check"] = error_result
    error_detected = error_result["error_detected"]
    if error_detected:
        error_message = f"Error terms found in screen text: {', '.join(error_result['error_terms'])}"

    # 4. Change-detection contributes to confidence
    change_confidence = 0.6 if changes_detected else 0.0
    confidence_components.append(change_confidence)

    # Compute overall confidence
    # Weight: keyword match 60%, change detection 40%
    raw_confidence = (keyword_confidence * 0.6) + (change_confidence * 0.4)

    # Penalize if errors detected
    if error_detected:
        raw_confidence *= 0.3

    confidence = round(min(max(raw_confidence, 0.0), 1.0), 4)

    # Verified if confidence >= 0.5 and no errors
    verified = confidence >= 0.5 and not error_detected

    # If the action didn't change anything but we expected it to, lower confidence
    if not changes_detected and keyword_confidence < 0.3:
        verified = False

    details["action_taken"] = req.action_taken
    details["timestamp"] = datetime.now(timezone.utc).isoformat()

    logger.info(
        "Verification task=%s verified=%s confidence=%.3f changes=%s errors=%s",
        req.task_id, verified, confidence, changes_detected, error_detected,
    )

    return VerifyResponse(
        verified=verified,
        confidence=confidence,
        changes_detected=changes_detected,
        error_detected=error_detected,
        error_message=error_message,
        verification_details=details,
    )


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8009, reload=False)
