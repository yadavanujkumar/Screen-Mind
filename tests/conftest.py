"""Shared pytest fixtures for Screen-Mind tests."""
from __future__ import annotations

import base64
import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchval = AsyncMock(return_value=None)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool


@pytest.fixture
def sample_task() -> dict[str, Any]:
    return {
        "id": "task-123",
        "user_id": "user-456",
        "task_description": "Open browser and search for weather",
        "status": "pending",
        "start_time": None,
        "end_time": None,
        "created_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def sample_screenshot_b64() -> str:
    """Return a valid 1x1 pixel PNG as base64 string."""
    img = Image.new("RGB", (1, 1), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
