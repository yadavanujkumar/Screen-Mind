# Screen-Mind System Architecture

## Overview

Screen-Mind is a microservices-based autonomous AI agent platform. Each concern — screen capture, vision, reasoning, action execution, memory, observability, and explainability — runs as an independent FastAPI service. Services communicate over HTTP/REST, share state through PostgreSQL and Redis, and are co-ordinated by the API Gateway.

```
External Client
     │
     ▼
┌─────────────────────────────────────────────────────┐
│               API Gateway  :8000                    │
│  Auth enforcement · Rate limiting · WebSocket relay │
└───────────────────────┬─────────────────────────────┘
                        │
            ┌───────────▼──────────┐
            │   Auth Service :8001 │  ← User & key management
            └──────────────────────┘
                        │
            ┌───────────▼──────────┐
            │  Task Planner :8006  │  ← Task lifecycle, Redis plan cache
            └───────────┬──────────┘
                        │
            ┌───────────▼──────────┐
            │  Task Queue  :8012   │  ← Priority queue (Redis-backed)
            └───────────┬──────────┘
                        │
         ┌──────────────▼──────────────────────────┐
         │                AGENT LOOP                │
         │  Observe→Understand→Reason→Plan→         │
         │  Act→Verify→Log→Learn                    │
         └──┬──────────┬──────────┬─────────────────┘
            │          │          │
     Screen Capture  Vision   State Builder
       :8002          :8003      :8004
            └──────────┴──────────┘
                        │
            ┌───────────▼──────────┐
            │  LLM Reasoning :8005 │  ← GPT-4o / Ollama
            └───────────┬──────────┘
                        │
            ┌───────────▼──────────┐
            │ Action Execution:8007│  ← PyAutoGUI
            └───────────┬──────────┘
                        │
            ┌───────────▼──────────┐
            │  Verification  :8009 │
            └──┬──────┬───────┬───┘
               │      │       │
           Memory  Observ. Explain.
           :8008   :8010   :8011
               │      │       │
            PostgreSQL · Redis · FAISS
```

---

## Core Agent Loop

The `AgentLoop` class in `agent/core_loop.py` drives each iteration:

### 1 — Observe
Calls `GET /capture` on the Screen Capture service. Returns a base64-encoded PNG of the full screen and an ISO-8601 timestamp.

### 2 — Understand
Posts the screenshot to the Vision service (`POST /analyze`). EasyOCR extracts all visible text; the detector identifies interactive elements (buttons, inputs, links) with bounding boxes. The State Builder aggregates these into a `ScreenState` object containing `screen_type`, `state_summary`, `key_text[]`, and `interactive_elements[]`.

### 3 — Reason
Posts the `ScreenState`, the task goal, step number, memory context, and prior actions to the LLM Reasoning service (`POST /reason`). The LLM returns a JSON payload with:
- `decision` — human-readable action description
- `reason` — rationale
- `alternatives[]` — other actions considered
- `confidence` — 0–1 float
- `next_action` — structured action object
- `expected_outcome` — what should be true after the action
- `task_complete` — boolean termination signal

### 4 — Plan
Posts the reasoning output to the Task Planner (`POST /plan`). The planner calls the LLM to expand the decision into an ordered list of sub-steps, persists them to PostgreSQL, and caches the plan in Redis (TTL configurable via `PLAN_CACHE_TTL`). The first sub-step action is extracted for execution.

### 5 — Act
Posts the action to the Action Execution service (`POST /execute`). The service validates the action type against `ALLOWED_ACTIONS`, validates coordinates against screen bounds, blocks dangerous keys if `BLOCK_DANGEROUS_KEYS=true`, and runs the action via PyAutoGUI. In `SAFE_MODE=true` the action is logged without execution.

### 6 — Verify
Takes a fresh screenshot and calls the Verification service (`POST /verify`) with the screenshot and the expected outcome string. The service returns `verified: bool` and a confidence score.

### 7 — Log
Posts the full step data (action, timing, verification result, OCR text, detected elements, reasoning) to the Observability service (`POST /log`), which persists to PostgreSQL and emits Prometheus metrics.

### 8 — Learn
Posts a summary of the step to the Memory service (`POST /store`). The sentence-transformer model (`all-MiniLM-L6-v2`) encodes the content to a 384-dimensional vector. Long-term memories are indexed in FAISS for future similarity search; short-term memories are stored in PostgreSQL only.

The loop repeats up to `max_steps` (default 50) iterations, terminating early when `task_complete` is true or a fatal error occurs.

---

## Service Communication

| Pattern | Used For |
|---------|---------|
| HTTP REST (httpx async) | All inter-service calls |
| PostgreSQL (asyncpg) | Persistent state — tasks, actions, users, logs, memory, metrics |
| Redis (aioredis) | Task queue, plan cache, future pub/sub |
| FAISS (in-process) | Vector similarity search within the Memory service |
| WebSocket (FastAPI) | Live task status streaming via the API Gateway |
| Slack Events API | Remote chat ingestion via Slack Adapter and Conversation service |
| Prometheus scrape | Metrics collection from `/metrics` endpoints |
| OpenTelemetry / Jaeger | Distributed tracing (spans emitted by the API Gateway) |

---

## Remote Chat (Slack) Architecture

```
Slack User Message
      │
      ▼
Slack Events API ──▶ Slack Adapter :8014
                          │
                          ├─ verify Slack signature (HMAC-SHA256)
                          ├─ map channel -> conversation session
                          ▼
                 Conversation Service :8013
                          │
                          ├─ classify intent (question/direction/clarification)
                          └─ optionally execute direction as task
                          ▼
                    Task Planner :8006
                          │
                          ▼
                    Agent Loop Services
                          │
                          ▼
             Slack Adapter posts reply back to channel
```

The Slack adapter is stateless except for an in-memory channel-to-session map used to keep conversation context per Slack channel. For production multi-replica deployments, move this mapping to Redis to preserve sticky sessions across instances.

---

## Data Flow for a Typical Task

```
1.  Client → POST /api/v1/tasks          (API Gateway → Task Planner)
2.  Task Planner → INSERT tasks          (PostgreSQL)
3.  Client → POST /api/v1/tasks/{id}/execute  (API Gateway → Agent Orchestrator)
4.  Agent Orchestrator → AgentLoop.run()
5.  Loop iteration:
    a. Screen Capture → mss screenshot
    b. Vision → EasyOCR + element detection
    c. State Builder → ScreenState
    d. LLM Reasoning → next_action JSON
    e. Task Planner → plan steps (Redis cache)
    f. Action Execution → PyAutoGUI
    g. Verification → screenshot diff
    h. Observability → metrics + logs
    i. Memory → FAISS vector store
6.  Loop exits (task_complete or max_steps)
7.  Task Planner → UPDATE tasks SET status='COMPLETED'
8.  Explainability → aggregate step rationale
9.  Client ← GET /api/v1/tasks/{id}/explainability
```

---

## Database Schema

All tables live in a single PostgreSQL database (`screenmind`).

| Table | Purpose |
|-------|---------|
| `users` | User accounts — UUID PK, username, email, HMAC-SHA256 API key hash, role, active flag |
| `tasks` | Task records — UUID PK, description, status enum, start/end times |
| `actions` | Executed actions — type, coordinates/text payload, success flag, timestamp |
| `logs` | Service log entries — severity, message, linked task |
| `memory` | Agent memory entries — content text, embedding JSONB, importance score, memory type |
| `metrics` | Per-step performance — step time, model latency, success rate |
| `explainability_logs` | Per-step decision rationale — goal, decision, reason, alternatives, confidence |
| `audit_logs` | Security audit — user, action, resource, IP address |

All foreign keys reference `tasks(id)` with `ON DELETE CASCADE` (or `SET NULL` for `users`). Indexes are defined on every `task_id` foreign key column.

---

## Security Architecture

### Authentication Flow

```
Client request → API Gateway
  → extract X-API-Key header
  → HMAC-SHA256(secret, api_key) → lookup hash in users table
  → if valid & active → allow; attach user context
  → if invalid → 401; if inactive → 403
```

API keys are 256-bit URL-safe random tokens (`secrets.token_urlsafe(32)`). Only the HMAC hash is stored. The plaintext key is returned exactly once at registration.

### Role-Based Access Control

| Role | Permissions |
|------|------------|
| `admin` | All operations including user management |
| `operator` | Create and execute tasks, read all data |
| `viewer` | Read-only access to task status and reports |

### Action Sandboxing

- `SAFE_MODE=true` — all actions are validated and logged, but PyAutoGUI calls are skipped
- `ALLOWED_ACTIONS` — comma-separated allowlist of action types; unrecognised types return 403
- `ALLOWED_APP_NAMES` — allowlist for `OPEN_APPLICATION`; empty = feature disabled
- `BLOCK_DANGEROUS_KEYS` — blocks `ctrl+alt+delete`, `alt+f4`, `win+l`, `ctrl+shift+esc`
- `validate_coordinates` — rejects coordinates outside the physical screen bounds
- `validate_url` — rejects non-HTTP/HTTPS schemes for `OPEN_WEBSITE`

---

## Observability Stack

```
Services ──/metrics──▶ Prometheus :9090
                              │
                        Grafana :3000  (dashboards)

Services ──JSON logs──▶ Logstash ──▶ Elasticsearch :9200 ──▶ Kibana :5601

API Gateway ──spans──▶ Jaeger :6831 (UDP) ──▶ Jaeger UI :16686
```

The Grafana dashboard (`monitoring/grafana/dashboards/dashboard.json`) provides panels for:
- Request throughput and latency (p50/p95/p99)
- LLM token consumption and call latency by provider/model
- Action execution counts by type and status
- Task creation and completion rates
- Active in-flight LLM calls (gauge)

---

## Explainability

For each agent loop step the `explainability_logs` table records:

- `screen_text` — raw OCR output
- `detected_elements` — bounding boxes and labels
- `goal` — the task goal at this step
- `decision` — chosen action description
- `reason` — LLM rationale
- `alternatives` — other actions the LLM considered
- `confidence_score` — 0–1 float from the LLM

The Explainability service aggregates these records per task and returns a human-readable report. The API Gateway proxies this at `GET /api/v1/tasks/{task_id}/explainability`.

---

## Scaling

### Horizontal (stateless services)

API Gateway, LLM Reasoning, Action Execution, Vision, Screen Capture, and State Builder are stateless and can be scaled horizontally. Each Kubernetes deployment has a `HorizontalPodAutoscaler` targeting 70% CPU utilisation.

### Vertical (stateful services)

PostgreSQL and the Memory service (FAISS in-process) should be scaled vertically or migrated to managed services (e.g. AWS RDS, Pinecone) for production workloads.

### Redis

Use Redis Cluster or a managed service (ElastiCache, Upstash) for high availability of the task queue and plan cache.

### LLM Throughput

For high-volume deployments, switch `LLM_PROVIDER=ollama` and run a GPU-backed Ollama cluster, or use the OpenAI Batch API for non-real-time tasks.
