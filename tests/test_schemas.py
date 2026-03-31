"""Tests for Pydantic schemas in shared/models/schemas.py."""
from __future__ import annotations

import pytest
from datetime import datetime

from shared.models.schemas import (
    ActionRequest,
    ActionType,
    AgentDecision,
    LogLevel,
    MemoryEntry,
    MetricSnapshot,
    TaskCreate,
    TaskResponse,
    TaskStatus,
    UserRole,
)


def test_task_status_enum():
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.RUNNING == "running"
    assert TaskStatus.COMPLETED == "completed"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.CANCELLED == "cancelled"


def test_action_type_enum():
    expected = [
        "move_mouse", "click", "double_click", "right_click",
        "type_text", "press_key", "scroll_up", "scroll_down",
        "drag_and_drop", "open_application", "open_website", "wait",
        "take_screenshot",
    ]
    actual = [a.value for a in ActionType]
    assert sorted(actual) == sorted(expected)
    assert len(actual) == 13


def test_action_request_valid_click():
    req = ActionRequest(action_type=ActionType.CLICK, coordinates=[100, 200])
    assert req.action_type == ActionType.CLICK.value
    assert req.coordinates == [100, 200]
    assert req.text is None


def test_action_request_type_text():
    req = ActionRequest(action_type=ActionType.TYPE_TEXT, text="hello world")
    assert req.text == "hello world"
    assert req.coordinates is None


def test_action_request_drag():
    req = ActionRequest(
        action_type=ActionType.DRAG_AND_DROP,
        coordinates=[10, 20, 100, 200],
    )
    assert len(req.coordinates) == 4


def test_action_request_open_website():
    req = ActionRequest(action_type=ActionType.OPEN_WEBSITE, url="https://example.com")
    assert req.url == "https://example.com"


def test_task_create():
    task = TaskCreate(task_description="Do something", user_id="user-1")
    assert task.task_description == "Do something"
    assert task.user_id == "user-1"


def test_agent_decision():
    decision = AgentDecision(
        step_number=4,
        screen_text="Login Page",
        detected_buttons=["Login", "Register"],
        goal="Download bank statement",
        decision="Click Login",
        reason="User must login before downloading statement",
        alternatives=["Register instead"],
        confidence_score=0.92,
    )
    assert decision.step_number == 4
    assert decision.confidence_score == 0.92
    assert "Login" in decision.detected_buttons


def test_memory_entry():
    entry = MemoryEntry(
        id="mem-1",
        task_id="task-1",
        content="User logged in successfully",
        importance_score=0.8,
        memory_type="long_term",
        timestamp=datetime.now(),
    )
    assert entry.memory_type == "long_term"
    assert entry.importance_score == 0.8


def test_metric_snapshot():
    snap = MetricSnapshot(
        task_id="task-1",
        step_time=1.23,
        model_latency=0.45,
        success_rate=0.95,
        timestamp=datetime.now(),
    )
    assert snap.success_rate == 0.95


def test_log_level_enum():
    assert LogLevel.DEBUG == "debug"
    assert LogLevel.ERROR == "error"


def test_user_role_enum():
    assert UserRole.ADMIN == "admin"
    assert UserRole.OPERATOR == "operator"
    assert UserRole.VIEWER == "viewer"
