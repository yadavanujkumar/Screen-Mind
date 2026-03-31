"""Unit tests for the Conversation Service."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.conversation.main import (
    _mock_llm_response,
    _sessions,
    app,
)


@pytest.fixture(autouse=True)
def clear_sessions():
    """Reset in-memory session store before each test."""
    _sessions.clear()
    yield
    _sessions.clear()


client = TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "conversation"
    assert data["active_sessions"] == 0


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def test_create_session():
    resp = client.post("/sessions", json={"user_id": "user-1"})
    assert resp.status_code == 201
    data = resp.json()
    assert "session_id" in data
    assert data["user_id"] == "user-1"
    assert data["message_count"] == 0


def test_create_session_with_title():
    resp = client.post("/sessions", json={"user_id": "user-2", "title": "My test session"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"]


def test_get_session_not_found():
    resp = client.get("/sessions/nonexistent-id")
    assert resp.status_code == 404


def test_get_session_after_creation():
    # Create
    create_resp = client.post("/sessions", json={"user_id": "user-3"})
    session_id = create_resp.json()["session_id"]

    # Get
    get_resp = client.get(f"/sessions/{session_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["session_id"] == session_id
    assert data["user_id"] == "user-3"
    assert data["messages"] == []
    assert data["task_ids"] == []


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


def test_send_message_session_not_found():
    resp = client.post("/sessions/bad-id/messages", json={"content": "Hello"})
    assert resp.status_code == 404


@patch("services.conversation.main._call_llm", new_callable=AsyncMock)
def test_send_message_question_intent(mock_llm):
    mock_llm.return_value = {
        "reply": "I am Screen-Mind, here to help!",
        "intent": "question",
        "task_description": None,
        "requires_execution": False,
    }

    create_resp = client.post("/sessions", json={"user_id": "user-4"})
    session_id = create_resp.json()["session_id"]

    msg_resp = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "What can you do?"},
    )
    assert msg_resp.status_code == 200
    data = msg_resp.json()
    assert data["intent"] == "question"
    assert data["reply"] == "I am Screen-Mind, here to help!"
    assert data["requires_execution"] is False


@patch("services.conversation.main._call_llm", new_callable=AsyncMock)
def test_send_message_direction_intent(mock_llm):
    mock_llm.return_value = {
        "reply": "I'll open Chrome for you right away.",
        "intent": "direction",
        "task_description": "Open Chrome browser",
        "requires_execution": True,
    }

    create_resp = client.post("/sessions", json={"user_id": "user-5"})
    session_id = create_resp.json()["session_id"]

    msg_resp = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "Open Chrome browser"},
    )
    assert msg_resp.status_code == 200
    data = msg_resp.json()
    assert data["intent"] == "direction"
    assert data["task_description"] == "Open Chrome browser"
    assert data["requires_execution"] is True


@patch("services.conversation.main._call_llm", new_callable=AsyncMock)
def test_message_history_accumulated(mock_llm):
    mock_llm.return_value = {
        "reply": "Sure!",
        "intent": "question",
        "task_description": None,
        "requires_execution": False,
    }

    create_resp = client.post("/sessions", json={"user_id": "user-6"})
    session_id = create_resp.json()["session_id"]

    client.post(f"/sessions/{session_id}/messages", json={"content": "Hello"})
    client.post(f"/sessions/{session_id}/messages", json={"content": "How are you?"})

    get_resp = client.get(f"/sessions/{session_id}")
    data = get_resp.json()
    # 2 user + 2 assistant = 4 messages
    assert data["message_count"] == 4
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Execute direction
# ---------------------------------------------------------------------------


def test_execute_direction_session_not_found():
    resp = client.post("/sessions/bad-id/execute", json={})
    assert resp.status_code == 404


@patch("services.conversation.main._create_task", new_callable=AsyncMock)
@patch("services.conversation.main._call_llm", new_callable=AsyncMock)
def test_execute_direction_no_messages(mock_llm, mock_create_task):
    create_resp = client.post("/sessions", json={"user_id": "user-7"})
    session_id = create_resp.json()["session_id"]

    resp = client.post(f"/sessions/{session_id}/execute", json={})
    assert resp.status_code == 400


@patch("services.conversation.main._create_task", new_callable=AsyncMock)
@patch("services.conversation.main._call_llm", new_callable=AsyncMock)
def test_execute_direction_creates_task(mock_llm, mock_create_task):
    mock_llm.return_value = {
        "reply": "Opening Chrome for you!",
        "intent": "direction",
        "task_description": "Open Chrome",
        "requires_execution": True,
    }
    mock_create_task.return_value = {"task_id": "task-abc-123", "status": "PENDING"}

    create_resp = client.post("/sessions", json={"user_id": "user-8"})
    session_id = create_resp.json()["session_id"]

    # Send a direction message
    client.post(f"/sessions/{session_id}/messages", json={"content": "Open Chrome"})

    # Execute the direction
    exec_resp = client.post(f"/sessions/{session_id}/execute", json={})
    assert exec_resp.status_code == 200
    data = exec_resp.json()
    assert data["task_id"] == "task-abc-123"
    assert data["session_id"] == session_id
    mock_create_task.assert_called_once()

    # Verify task_id is stored in session
    get_resp = client.get(f"/sessions/{session_id}")
    assert "task-abc-123" in get_resp.json()["task_ids"]


def test_execute_invalid_message_index():
    create_resp = client.post("/sessions", json={"user_id": "user-9"})
    session_id = create_resp.json()["session_id"]

    # Try to execute with an out-of-range index
    resp = client.post(f"/sessions/{session_id}/execute", json={"message_index": 999})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Mock LLM response heuristic
# ---------------------------------------------------------------------------


def test_mock_llm_response_detects_direction():
    result = _mock_llm_response("Open Chrome browser")
    assert result["intent"] == "direction"
    assert result["requires_execution"] is True
    assert result["task_description"] is not None


def test_mock_llm_response_detects_question():
    result = _mock_llm_response("What is Screen-Mind?")
    assert result["intent"] == "question"
    assert result["requires_execution"] is False
