# Screen-Mind API Reference

All external requests are routed through the **API Gateway** on port **8000**.  
Every protected endpoint requires the `X-API-Key` header.  
Internal service endpoints (ports 8001–8012) are documented here for developers but should not be exposed publicly in production.

---

## Table of Contents

1. [API Gateway — External API (:8000)](#api-gateway-8000)
2. [Auth Service (:8001)](#auth-service-8001)
3. [Task Planner (:8006)](#task-planner-8006)
4. [LLM Reasoning (:8005)](#llm-reasoning-8005)
5. [Action Execution (:8007)](#action-execution-8007)
6. [Memory (:8008)](#memory-service-8008)
7. [Explainability (:8011)](#explainability-service-8011)
8. [Screen Capture (:8002)](#screen-capture-8002)
9. [Common Responses](#common-responses)

---

## API Gateway (:8000)

Base URL: `http://localhost:8000`

### Authentication

All routes (except `/health` and `/metrics`) require:

```
X-API-Key: <plaintext api key>
```

The gateway validates the key by computing `HMAC-SHA256(API_KEY_SECRET, api_key)` and looking up the hash in the `users` table.

---

### GET /health

Health check. No authentication required.

**Response 200**
```json
{
  "status": "healthy",
  "service": "api-gateway"
}
```

---

### GET /metrics

Prometheus metrics in text exposition format. No authentication required.

**Response 200** — `text/plain; version=0.0.4`

---

### POST /api/v1/tasks

Create a new task.

**Request Headers**
```
X-API-Key: <api-key>
Content-Type: application/json
```

**Request Body**
```json
{
  "task_description": "Open browser and search for weather in New York",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "priority": "normal"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_description` | string | ✓ | Plain-language description of the task |
| `user_id` | string (UUID) | ✓ | UUID of the requesting user |
| `priority` | string | — | `low`, `normal`, `high` (default: `normal`) |

**Response 201**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "...",
  "description": "Open browser and search for weather in New York",
  "status": "PENDING",
  "error_message": null,
  "created_at": "2024-01-15T10:30:00.000Z",
  "updated_at": "2024-01-15T10:30:00.000Z"
}
```

---

### GET /api/v1/tasks/{task_id}

Retrieve full task details.

**Path Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string (UUID) | Task identifier |

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "...",
  "description": "...",
  "status": "RUNNING",
  "error_message": null,
  "created_at": "2024-01-15T10:30:00.000Z",
  "updated_at": "2024-01-15T10:31:05.000Z"
}
```

**Task Status Values**

| Status | Meaning |
|--------|---------|
| `PENDING` | Task created, not yet started |
| `PLANNING` | Task planner generating steps |
| `RUNNING` | Agent loop is executing |
| `PAUSED` | Execution suspended |
| `COMPLETED` | Task finished successfully |
| `FAILED` | Task failed |
| `CANCELLED` | Task cancelled by user |

---

### GET /api/v1/tasks/{task_id}/status

Lightweight status poll.

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "RUNNING"
}
```

---

### POST /api/v1/tasks/{task_id}/execute

Start the agent loop for a task. Proxies to the agent orchestrator.

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Agent loop started"
}
```

---

### GET /api/v1/tasks/{task_id}/explainability

Retrieve the explainability report — per-step reasoning, decisions, alternatives, and confidence scores.

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "steps": [
    {
      "step_number": 1,
      "screen_text": "Google Chrome - New Tab",
      "detected_elements": [
        {"label": "address_bar", "bbox": [200, 50, 900, 80]}
      ],
      "goal": "Search for weather in New York",
      "decision": "Click on the address bar",
      "reason": "The address bar is visible and empty; clicking it will allow URL/search input",
      "alternatives": ["Press Ctrl+L to focus address bar", "Use keyboard shortcut"],
      "confidence_score": 0.92,
      "timestamp": "2024-01-15T10:31:06.000Z"
    }
  ],
  "total_steps": 7,
  "task_complete": true
}
```

---

### GET /api/v1/tasks/{task_id}/actions

List all actions executed for a task.

**Response 200**
```json
[
  {
    "id": 1,
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "action_type": "CLICK",
    "payload": {"coordinates": [500, 65]},
    "success": true,
    "message": "Clicked at (500, 65)",
    "executed_at": "2024-01-15T10:31:06.500Z"
  }
]
```

---

### GET /api/v1/metrics

Aggregated observability metrics (proxied from the Observability service).

**Response 200**
```json
{
  "total_tasks": 42,
  "completed_tasks": 38,
  "failed_tasks": 4,
  "avg_step_time_ms": 1240,
  "avg_model_latency_ms": 890
}
```

---

### GET /api/v1/memory

Retrieve memory entries (proxied from the Memory service). Supports `?query=` for semantic search.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Semantic similarity query |
| `task_id` | string | Filter by task |
| `memory_type` | string | `short_term`, `long_term`, `failure`, `important_action` |

---

### WebSocket /ws/tasks/{task_id}/live

Stream live status changes for a task.

**Authentication** — Pass the API key as either:
- Header: `X-API-Key: <key>`
- Query parameter: `?api_key=<key>`

**Messages received** (JSON)
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "RUNNING"
}
```

The connection closes automatically when the task reaches a terminal state (`COMPLETED`, `FAILED`, `CANCELLED`).

**Close codes**

| Code | Meaning |
|------|---------|
| 4001 | Missing or invalid API key |
| 4503 | Database unavailable |

---

## Auth Service (:8001)

Base URL: `http://localhost:8001`

> In production, user management calls should be made through the API Gateway or a private network — the Auth service should not be internet-facing.

---

### POST /auth/register

Create a new user and issue an API key.

**Request Body**
```json
{
  "username": "alice",
  "email": "alice@example.com",
  "role": "operator"
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `username` | string | ✓ | 3–64 characters, must be unique |
| `email` | string | — | Must be unique if provided |
| `role` | string | — | `admin`, `operator`, `viewer` (default: `operator`) |

**Response 201**
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "alice",
  "api_key": "xK2mNpQ8rT...",
  "role": "operator",
  "message": "User registered. Store the api_key securely — it will not be shown again."
}
```

> ⚠️ The `api_key` is returned **once only**. Store it immediately.

**Response 409** — Username or email already exists
```json
{"detail": "A user with that username already exists"}
```

---

### POST /auth/validate

Validate an API key and retrieve user metadata. Used internally by the gateway.

**Request Body**
```json
{
  "api_key": "xK2mNpQ8rT..."
}
```

**Response 200 — valid key**
```json
{
  "valid": true,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "alice",
  "role": "operator"
}
```

**Response 200 — invalid or inactive key**
```json
{
  "valid": false,
  "user_id": null,
  "username": null,
  "role": null
}
```

---

### POST /auth/rotate-key

Invalidate the current API key and issue a new one.

**Request Body**
```json
{
  "api_key": "xK2mNpQ8rT..."
}
```

**Response 200**
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "alice",
  "new_api_key": "yP9wLqR3sV...",
  "message": "API key rotated. Store the new key securely — it will not be shown again."
}
```

---

### GET /auth/users/{user_id}

Retrieve user details. Requires an `admin` API key in `X-API-Key`.

**Response 200**
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "alice",
  "email": "alice@example.com",
  "role": "operator",
  "is_active": true
}
```

---

## Task Planner (:8006)

Base URL: `http://localhost:8006`

---

### POST /tasks

Create a task record.

**Request Body**
```json
{
  "task_description": "Take a screenshot of the desktop",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response 201** — Task object (see schema above)

---

### GET /tasks

List all tasks. Optionally filter by user.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_id` | string | Filter to a specific user's tasks |

**Response 200** — Array of task objects

---

### GET /tasks/{task_id}

Get a single task by ID.

**Response 200** — Task object  
**Response 404** — `{"detail": "Task not found"}`

---

### GET /tasks/{task_id}/status

Get task status only.

**Response 200**
```json
{"task_id": "...", "status": "RUNNING"}
```

---

### PUT /tasks/{task_id}/status

Update task status.

**Request Body**
```json
{
  "status": "COMPLETED",
  "error_message": null
}
```

**Response 200** — Updated task object

---

### DELETE /tasks/{task_id}

Cancel a task (sets status to `CANCELLED`).

**Response 200** — Updated task object

---

### POST /plan

Generate an ordered step-by-step plan using the LLM Reasoning service. Plans are cached in Redis.

**Request Body**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "goal": "Search for weather in New York",
  "screen_state": {
    "screen_type": "browser",
    "state_summary": "Chrome browser is open on a new tab",
    "key_text": ["New Tab", "address bar"],
    "interactive_elements": [{"label": "address_bar", "bbox": [200, 50, 900, 80]}],
    "ocr_text": "New Tab"
  },
  "reasoning": {}
}
```

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "goal": "Search for weather in New York",
  "steps": [
    {
      "step_number": 1,
      "description": "Click on the browser address bar",
      "action": {"action_type": "CLICK", "coordinates": [500, 65]}
    },
    {
      "step_number": 2,
      "description": "Type the search query",
      "action": {"action_type": "TYPE_TEXT", "text": "weather New York"}
    },
    {
      "step_number": 3,
      "description": "Press Enter to search",
      "action": {"action_type": "PRESS_KEY", "key": "enter"}
    }
  ],
  "estimated_steps": 3,
  "model_used": "gpt-4o",
  "latency_ms": 1243.5
}
```

---

### GET /tasks/{task_id}/steps

Retrieve planned steps. Returns from Redis cache if available, otherwise from PostgreSQL.

**Response 200**
```json
{
  "task_id": "...",
  "steps": [...],
  "source": "cache"
}
```

---

## LLM Reasoning (:8005)

Base URL: `http://localhost:8005`

---

### POST /reason

Ask the LLM for the best next action given the current screen state.

**Request Body**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "goal": "Search for weather in New York",
  "screen_state": {
    "screen_type": "browser",
    "state_summary": "Chrome new tab page",
    "key_text": ["New Tab"],
    "interactive_elements": [],
    "ocr_text": "New Tab"
  },
  "step_number": 1,
  "memory_context": [],
  "previous_actions": []
}
```

**Response 200**
```json
{
  "decision": "Click on the address bar to begin typing",
  "reason": "The address bar is the entry point for navigation; clicking it focuses input",
  "alternatives": ["Press Ctrl+L", "Use keyboard shortcut Alt+D"],
  "confidence": 0.95,
  "next_action": {
    "action_type": "CLICK",
    "coordinates": [500, 65]
  },
  "expected_outcome": "The address bar is focused and ready for text input",
  "task_complete": false,
  "model_used": "gpt-4o",
  "latency_ms": 872.3
}
```

If no LLM provider is configured a mock response is returned (no error).

---

### POST /explain

Generate a plain-English explanation of a specific decision.

**Request Body**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "decision": "CLICK address bar",
  "context": {
    "screen_type": "browser",
    "goal": "Search for weather in New York"
  }
}
```

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "explanation": "The agent clicked the address bar because it is the standard way to begin navigation in a browser. The goal requires searching for weather information, which requires entering a query in the address bar first.",
  "model_used": "gpt-4o",
  "latency_ms": 634.1
}
```

---

### GET /health

**Response 200**
```json
{
  "status": "healthy",
  "provider": "openai",
  "model": "gpt-4o"
}
```

---

## Action Execution (:8007)

Base URL: `http://localhost:8007`

---

### POST /execute

Execute a mouse or keyboard action.

**Request Body**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "action": {
    "action_type": "CLICK",
    "coordinates": [500, 65],
    "text": null,
    "key": null,
    "app_name": null,
    "url": null,
    "seconds": null,
    "end_coordinates": null
  }
}
```

**Action type examples**

| action_type | Required fields | Optional fields |
|------------|-----------------|-----------------|
| `CLICK` | — | `coordinates` |
| `DOUBLE_CLICK` | — | `coordinates` |
| `RIGHT_CLICK` | — | `coordinates` |
| `MOVE_MOUSE` | `coordinates` | — |
| `TYPE_TEXT` | `text` | — |
| `PRESS_KEY` | `key` | — |
| `SCROLL_UP` | — | `coordinates` |
| `SCROLL_DOWN` | — | `coordinates` |
| `DRAG_AND_DROP` | `coordinates`, `end_coordinates` | — |
| `OPEN_APPLICATION` | `app_name` | — |
| `OPEN_WEBSITE` | `url` | — |
| `WAIT` | — | `seconds` (default 1.0, max 60) |
| `TAKE_SCREENSHOT` | — | — |

**Response 200**
```json
{
  "success": true,
  "action_type": "CLICK",
  "message": "Clicked at (500, 65)",
  "timestamp": "2024-01-15T10:31:06.500Z"
}
```

**Response 400** — Validation error (missing required field, invalid coordinates)  
**Response 403** — Action blocked by `ALLOWED_ACTIONS`, `BLOCK_DANGEROUS_KEYS`, or `ALLOWED_APP_NAMES`  
**Response 500** — Execution error (see server logs)

---

### GET /actions/{task_id}

List all actions recorded for a task.

**Response 200** — Array of action records
```json
[
  {
    "id": 1,
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "action_type": "CLICK",
    "payload": {"coordinates": [500, 65]},
    "success": true,
    "message": "Clicked at (500, 65)",
    "executed_at": "2024-01-15T10:31:06.500Z"
  }
]
```

---

### GET /health

**Response 200**
```json
{
  "status": "healthy",
  "safe_mode": false,
  "block_dangerous_keys": true,
  "allowed_actions": "all",
  "screen_size": {"width": 1920, "height": 1080},
  "database": true
}
```

---

## Memory Service (:8008)

Base URL: `http://localhost:8008`

---

### POST /store

Store a memory entry and compute its vector embedding.

**Request Body**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Step 3: Clicked the submit button. success=True goal=Submit form",
  "memory_type": "short_term",
  "importance_score": 0.8
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `task_id` | string | ✓ | — |
| `content` | string | ✓ | — |
| `memory_type` | string | ✓ | `short_term`, `long_term`, `failure`, `important_action` |
| `importance_score` | float | — | 0.0–1.0 (default 0.5) |

**Response 201**
```json
{
  "id": 42,
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Step 3: Clicked the submit button. success=True goal=Submit form",
  "memory_type": "short_term",
  "importance_score": 0.8,
  "created_at": "2024-01-15T10:31:06.500Z"
}
```

> Long-term memories are additionally indexed in FAISS for semantic retrieval.

---

### POST /retrieve

Retrieve memories by semantic similarity.

**Request Body**
```json
{
  "query": "How did the agent submit the form?",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "memory_type": "long_term",
  "top_k": 5
}
```

**Response 200**
```json
{
  "query": "How did the agent submit the form?",
  "results": [
    {
      "id": 42,
      "task_id": "...",
      "content": "Step 3: Clicked the submit button...",
      "memory_type": "long_term",
      "importance_score": 0.8,
      "similarity_score": 0.94,
      "created_at": "2024-01-15T10:31:06.500Z"
    }
  ],
  "total": 1
}
```

Falls back to PostgreSQL keyword search when the FAISS index is empty.

---

### GET /memory/{task_id}

Retrieve all memory entries for a task, ordered by creation time.

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "memories": [...],
  "total": 12
}
```

---

### DELETE /memory/{task_id}

Clear **short-term** memories for a task (long-term and failure entries are preserved).

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "deleted_count": 7,
  "message": "Short-term memories cleared"
}
```

---

### GET /memory/stats

Global memory statistics.

**Response 200**
```json
{
  "counts_by_type": {
    "short_term": 120,
    "long_term": 45,
    "failure": 8,
    "important_action": 22
  },
  "total": 195,
  "avg_importance_score": 0.67,
  "faiss_index_size": 45
}
```

---

## Explainability Service (:8011)

Base URL: `http://localhost:8011`

### GET /explain/{task_id}

Return the full explainability report for a task — all steps with their decision rationale.

**Response 200**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "steps": [
    {
      "step_number": 1,
      "screen_text": "New Tab",
      "detected_elements": [],
      "goal": "Search for weather in New York",
      "decision": "Click address bar",
      "reason": "Need to focus the address bar before typing",
      "alternatives": ["Press Ctrl+L"],
      "confidence_score": 0.95,
      "timestamp": "2024-01-15T10:31:06.000Z"
    }
  ],
  "total_steps": 1
}
```

---

## Screen Capture (:8002)

Base URL: `http://localhost:8002`

### GET /capture

Capture the current screen.

**Response 200**
```json
{
  "screenshot_b64": "<base64-encoded PNG>",
  "width": 1920,
  "height": 1080,
  "timestamp": "2024-01-15T10:31:05.000Z"
}
```

---

## Common Responses

### Error Response Format

All services return errors in this format:

```json
{
  "detail": "Human-readable error message"
}
```

### Standard HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | OK |
| 201 | Created |
| 400 | Bad Request — validation error |
| 401 | Unauthorized — missing or invalid API key |
| 403 | Forbidden — insufficient role or blocked action |
| 404 | Not Found |
| 409 | Conflict — duplicate username or email |
| 429 | Too Many Requests — rate limit exceeded (100 req/min) |
| 500 | Internal Server Error |
| 502 | Bad Gateway — upstream service error |
| 503 | Service Unavailable — database or dependency not ready |
| 504 | Gateway Timeout — upstream timed out |

### Rate Limiting

The API Gateway enforces **100 requests per minute per IP address**. Exceeding this returns HTTP 429:

```json
{
  "error": "Rate limit exceeded: 100 per 1 minute"
}
```

Configure the limit via the `RATE_LIMIT` environment variable (e.g. `"200/minute"`).
