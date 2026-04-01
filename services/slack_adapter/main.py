"""Slack Adapter Service for remote chat control."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

CONVERSATION_URL = os.getenv("CONVERSATION_URL", "http://conversation:8013")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
AUTO_EXECUTE_DIRECTIONS = os.getenv("AUTO_EXECUTE_DIRECTIONS", "true").lower() == "true"
SLACK_API_URL = "https://slack.com/api"
MAX_SLACK_TIMESTAMP_AGE = 60 * 5

app = FastAPI(title="Slack Adapter Service", version="1.0.0")

# Slack channel ID -> Conversation session_id
_channel_sessions: dict[str, str] = {}


def _verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return True
    if not timestamp or not signature:
        return False
    try:
        ts_value = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts_value) > MAX_SLACK_TIMESTAMP_AGE:
        return False
    basestring = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _get_or_create_session(channel_id: str, user_id: str) -> str:
    existing = _channel_sessions.get(channel_id)
    if existing:
        return existing
    payload = {"user_id": user_id, "title": f"Slack channel {channel_id}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{CONVERSATION_URL}/sessions", json=payload)
        response.raise_for_status()
        session_id = response.json()["session_id"]
        _channel_sessions[channel_id] = session_id
        return session_id


async def _send_message_to_conversation(session_id: str, text: str) -> dict[str, Any]:
    payload = {"role": "user", "content": text}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(f"{CONVERSATION_URL}/sessions/{session_id}/messages", json=payload)
        response.raise_for_status()
        return response.json()


async def _execute_direction(session_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(f"{CONVERSATION_URL}/sessions/{session_id}/execute", json={})
        response.raise_for_status()
        return response.json()


async def _post_to_slack(channel: str, text: str) -> None:
    if not SLACK_BOT_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {"channel": channel, "text": text}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{SLACK_API_URL}/chat.postMessage", json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise HTTPException(status_code=502, detail=f"Slack API error: {data.get('error', 'unknown_error')}")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "slack-adapter",
        "conversation_url": CONVERSATION_URL,
        "bot_token_configured": bool(SLACK_BOT_TOKEN),
        "signing_secret_configured": bool(SLACK_SIGNING_SECRET),
    }


@app.post("/slack/events")
async def slack_events(
    request: Request,
    x_slack_signature: str = Header(default="", alias="X-Slack-Signature"),
    x_slack_request_timestamp: str = Header(default="", alias="X-Slack-Request-Timestamp"),
) -> dict[str, Any]:
    raw_body = await request.body()
    if not _verify_slack_signature(raw_body, x_slack_request_timestamp, x_slack_signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = await request.json()
    payload_type = payload.get("type")

    if payload_type == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    if payload_type != "event_callback":
        return {"ok": True, "ignored": True}

    event = payload.get("event", {})
    if event.get("type") != "message" or event.get("subtype"):
        return {"ok": True, "ignored": True}

    channel = event.get("channel", "")
    user = event.get("user", "")
    text = (event.get("text") or "").strip()
    if not channel or not user or not text:
        return {"ok": True, "ignored": True}

    team_id = payload.get("team_id", "unknown-team")
    mapped_user_id = f"slack:{team_id}:{user}"
    session_id = await _get_or_create_session(channel, mapped_user_id)
    response = await _send_message_to_conversation(session_id, text)

    reply_text = response.get("reply", "Received.")
    if AUTO_EXECUTE_DIRECTIONS and response.get("intent") == "direction" and response.get("requires_execution"):
        task_info = await _execute_direction(session_id)
        task_id = task_info.get("task_id", "unknown")
        reply_text = f"{reply_text}\nTask started: `{task_id}`"

    await _post_to_slack(channel, reply_text)
    return {"ok": True}
