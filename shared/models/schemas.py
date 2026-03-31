from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionType(str, Enum):
    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    DRAG_AND_DROP = "drag_and_drop"
    OPEN_APPLICATION = "open_application"
    OPEN_WEBSITE = "open_website"
    WAIT = "wait"
    TAKE_SCREENSHOT = "take_screenshot"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class UserRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class ActionRequest(BaseModel):
    action_type: ActionType
    coordinates: Optional[list[int]] = Field(
        default=None,
        description="2 ints [x, y] for point actions or 4 ints [x1, y1, x2, y2] for drag",
    )
    text: Optional[str] = None
    key: Optional[str] = None
    app_name: Optional[str] = None
    url: Optional[str] = None
    seconds: Optional[float] = None

    model_config = {"use_enum_values": True}


class TaskCreate(BaseModel):
    task_description: str
    user_id: str


class TaskResponse(BaseModel):
    id: str
    user_id: str
    task_description: str
    status: TaskStatus
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    created_at: datetime

    model_config = {"use_enum_values": True}


class ActionResponse(BaseModel):
    id: str
    task_id: str
    action_type: ActionType
    coordinates: Optional[list[int]] = None
    text: Optional[str] = None
    status: str
    timestamp: datetime

    model_config = {"use_enum_values": True}


class ScreenState(BaseModel):
    screenshot_b64: str
    ocr_text: str
    detected_elements: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime


class AgentDecision(BaseModel):
    step_number: int
    screen_text: str
    detected_buttons: list[str] = Field(default_factory=list)
    goal: str
    decision: str
    reason: str
    alternatives: list[str] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0, le=1.0)


class MemoryEntry(BaseModel):
    id: str
    task_id: str
    content: str
    importance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    memory_type: str = "general"
    timestamp: datetime


class MetricSnapshot(BaseModel):
    task_id: str
    step_time: float
    model_latency: float
    success_rate: float
    timestamp: datetime
