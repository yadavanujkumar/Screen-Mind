"""Unit tests for Slack Adapter Service."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.slack_adapter.main import app

client = TestClient(app)


def _signature(secret: str, body: bytes, timestamp: str) -> str:
    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    digest = hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "slack-adapter"


def test_url_verification_without_signature_secret():
    payload = {"type": "url_verification", "challenge": "abc123"}
    resp = client.post("/slack/events", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


@patch("services.slack_adapter.main.SLACK_SIGNING_SECRET", "test-signing-secret")
def test_invalid_signature_rejected():
    payload = {"type": "url_verification", "challenge": "abc123"}
    raw = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    resp = client.post(
        "/slack/events",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": "v0=bad",
        },
    )
    assert resp.status_code == 401


@patch("services.slack_adapter.main.SLACK_SIGNING_SECRET", "test-signing-secret")
def test_valid_signature_allows_url_verification():
    payload = {"type": "url_verification", "challenge": "abc123"}
    raw = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    sig = _signature("test-signing-secret", raw, ts)
    resp = client.post(
        "/slack/events",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


@patch("services.slack_adapter.main._post_to_slack", new_callable=AsyncMock)
@patch("services.slack_adapter.main._send_message_to_conversation", new_callable=AsyncMock)
@patch("services.slack_adapter.main._get_or_create_session", new_callable=AsyncMock)
def test_message_event_forwarded_to_conversation(mock_get_session, mock_send_message, mock_post_slack):
    mock_get_session.return_value = "session-1"
    mock_send_message.return_value = {
        "reply": "I'll do that.",
        "intent": "question",
        "requires_execution": False,
    }
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", "channel": "C1", "user": "U1", "text": "Hello"},
    }
    resp = client.post("/slack/events", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_get_session.assert_awaited_once_with("C1", "slack:T1:U1")
    mock_send_message.assert_awaited_once_with("session-1", "Hello")
    mock_post_slack.assert_awaited_once_with("C1", "I'll do that.")


@patch("services.slack_adapter.main.AUTO_EXECUTE_DIRECTIONS", True)
@patch("services.slack_adapter.main._post_to_slack", new_callable=AsyncMock)
@patch("services.slack_adapter.main._execute_direction", new_callable=AsyncMock)
@patch("services.slack_adapter.main._send_message_to_conversation", new_callable=AsyncMock)
@patch("services.slack_adapter.main._get_or_create_session", new_callable=AsyncMock)
def test_direction_event_auto_executes(
    mock_get_session,
    mock_send_message,
    mock_execute,
    mock_post_slack,
):
    mock_get_session.return_value = "session-2"
    mock_send_message.return_value = {
        "reply": "Starting now.",
        "intent": "direction",
        "requires_execution": True,
    }
    mock_execute.return_value = {"task_id": "task-123"}
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", "channel": "C2", "user": "U2", "text": "Open chrome"},
    }
    resp = client.post("/slack/events", json=payload)
    assert resp.status_code == 200
    mock_execute.assert_awaited_once_with("session-2")
    mock_post_slack.assert_awaited_once_with("C2", "Starting now.\nTask started: `task-123`")
