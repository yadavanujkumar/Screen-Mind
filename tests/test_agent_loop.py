"""Unit tests for the core agent loop."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.core_loop import AgentLoop

SERVICE_URLS = {
    "screen_capture": "http://screen-capture:8002",
    "vision": "http://vision:8003",
    "state_builder": "http://state-builder:8004",
    "llm_reasoning": "http://llm-reasoning:8005",
    "task_planner": "http://task-planner:8006",
    "action_execution": "http://action-execution:8007",
    "memory": "http://memory:8008",
    "verification": "http://verification:8009",
    "observability": "http://observability:8010",
    "explainability": "http://explainability:8011",
}


def make_loop() -> AgentLoop:
    return AgentLoop(
        task_id="task-123",
        goal="Open browser",
        user_id="user-1",
        service_urls=SERVICE_URLS,
    )


def _mock_response(data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_observe_returns_screenshot():
    loop = make_loop()
    screenshot_data = {"screenshot_b64": "abc123", "width": 1920, "height": 1080, "timestamp": "2026-01-01T00:00:00Z"}
    loop.client.get = AsyncMock(return_value=_mock_response(screenshot_data))

    result = await loop.observe()

    assert result["screenshot_b64"] == "abc123"
    loop.client.get.assert_called_once()


@pytest.mark.asyncio
async def test_understand_parses_response():
    loop = make_loop()
    vision_data = {
        "ocr_text": "Login Page",
        "detected_elements": [{"type": "button", "text": "Login", "bbox": [10, 10, 100, 40], "confidence": 0.95}],
        "timestamp": "2026-01-01T00:00:00Z",
    }
    loop.client.post = AsyncMock(return_value=_mock_response(vision_data))

    result = await loop.understand("fake_b64_screenshot")

    assert result["ocr_text"] == "Login Page"
    assert len(result["detected_elements"]) == 1
    loop.client.post.assert_called_once()


@pytest.mark.asyncio
async def test_reason_returns_decision():
    loop = make_loop()
    reason_data = {
        "decision": "Click Login button",
        "reason": "User needs to log in first",
        "alternatives": ["Register instead"],
        "confidence": 0.92,
        "next_action": {"action_type": "click", "coordinates": [50, 25]},
        "expected_outcome": "Login form appears",
        "task_complete": False,
        "model_used": "gpt-4o",
        "latency_ms": 450.0,
    }
    loop.client.post = AsyncMock(return_value=_mock_response(reason_data))

    screen_state = {
        "screen_type": "login",
        "state_summary": "Login page visible",
        "key_text": ["Login Page"],
        "interactive_elements": [{"type": "button", "text": "Login"}],
        "ocr_text": "Login Page",
    }
    result = await loop.reason(screen_state)

    assert result["decision"] == "Click Login button"
    assert result["confidence"] == 0.92


@pytest.mark.asyncio
async def test_act_executes_action():
    loop = make_loop()
    action_data = {"success": True, "action_type": "click", "message": "Action executed", "timestamp": "2026-01-01T00:00:00Z"}
    loop.client.post = AsyncMock(return_value=_mock_response(action_data))

    action = {"action_type": "click", "coordinates": [100, 200]}
    result = await loop.act(action)

    assert result["success"] is True
    loop.client.post.assert_called_once()


@pytest.mark.asyncio
async def test_verify_returns_result():
    loop = make_loop()
    verify_data = {"verified": True, "confidence": 0.85, "changes_detected": True, "error_detected": False}

    # verify() calls observe() (GET) then calls verification service (POST)
    get_resp = _mock_response({"screenshot_b64": "new_b64", "timestamp": "2026-01-01T00:00:00Z"})
    post_resp = _mock_response(verify_data)

    loop.client.get = AsyncMock(return_value=get_resp)
    loop.client.post = AsyncMock(return_value=post_resp)

    result = await loop.verify("Login form appears")

    assert result["verified"] is True


@pytest.mark.asyncio
async def test_log_step_calls_observability():
    loop = make_loop()
    loop.client.post = AsyncMock(return_value=_mock_response({"status": "ok"}, 201))

    await loop.log_step({"step": 1, "action": "click", "success": True})

    loop.client.post.assert_called_once()


@pytest.mark.asyncio
async def test_learn_calls_memory():
    loop = make_loop()
    loop.client.post = AsyncMock(return_value=_mock_response({"id": "mem-1"}, 201))

    await loop.learn({"step": 1, "content": "Clicked login button"})

    loop.client.post.assert_called_once()


@pytest.mark.asyncio
async def test_full_loop_completes_on_task_complete():
    """Run loop with mocked services; simulate task_complete=True on first step."""
    loop = make_loop()

    capture_resp = _mock_response({"screenshot_b64": "b64img", "width": 1920, "height": 1080, "timestamp": "t"})
    vision_resp = _mock_response({
        "ocr_text": "Done", "detected_elements": [], "timestamp": "t",
        "screen_type": "generic", "state_summary": "Done screen",
        "key_text": [], "interactive_elements": [],
    })
    state_resp = _mock_response({
        "screen_type": "generic", "state_summary": "Done", "key_text": [], "interactive_elements": [],
    })
    reason_resp = _mock_response({
        "decision": "TASK_COMPLETE",
        "reason": "Task is done",
        "alternatives": [],
        "confidence": 1.0,
        "next_action": {"action_type": "wait", "seconds": 0},
        "expected_outcome": "nothing",
        "task_complete": True,
        "model_used": "mock",
        "latency_ms": 10.0,
    })
    action_resp = _mock_response({"success": True, "action_type": "wait", "message": "ok", "timestamp": "t"})
    verify_resp = _mock_response({"verified": True, "confidence": 1.0, "changes_detected": False, "error_detected": False})
    log_resp = _mock_response({"status": "ok"}, 201)
    mem_resp = _mock_response({"id": "m1"}, 201)

    call_count = {"get": 0, "post": 0}

    async def mock_get(url, **kwargs):
        call_count["get"] += 1
        return capture_resp

    plan_resp = _mock_response([{"step_number": 1, "description": "wait", "action": {"action_type": "wait", "seconds": 0}}])

    async def mock_post(url, **kwargs):
        call_count["post"] += 1
        if "vision" in url or "analyze" in url:
            return vision_resp
        if "state" in url or "build" in url:
            return state_resp
        if "reason" in url:
            return reason_resp
        if "plan" in url:
            return plan_resp
        if "execute" in url:
            return action_resp
        if "verify" in url:
            return verify_resp
        if "log" in url or "observability" in url:
            return log_resp
        if "store" in url or "memory" in url:
            return mem_resp
        if "explain" in url:
            return log_resp
        return log_resp

    loop.client.get = mock_get
    loop.client.post = mock_post

    result = await loop.run(max_steps=5)

    assert result["task_id"] == "task-123"
    assert result["steps_taken"] >= 1
    assert "final_status" in result
