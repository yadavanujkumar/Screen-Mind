# Screen-Mind: AI Computer Control Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-24+-blue.svg)](https://www.docker.com/)

## Overview

Screen-Mind is an enterprise-grade autonomous AI agent that observes a computer screen, understands its UI state, reasons about goals using large language models, and executes precise mouse and keyboard actions to accomplish complex tasks. It provides a complete microservices platform with authentication, observability, explainability, and a real-time dashboard — all deployable via Docker Compose or Kubernetes.

The agent follows a continuous **Observe → Understand → Reason → Plan → Act → Verify → Log → Learn** loop, enabling it to adapt to dynamic screen states and recover from unexpected situations.

## Architecture

```
[Client] → [API Gateway :8000] → [Auth :8001]
                    ↓
         [Task Queue :8012] → [Task Planner :8006]
                    ↓
         ┌──────────────────────────────────────┐
         │             AGENT LOOP               │
         │  Observe → Understand → Reason →     │
         │  Plan → Act → Verify → Log → Learn  │
         └──────────────────────────────────────┘
              ↓           ↓           ↓
    [Screen Capture]  [Vision]  [State Builder]
         :8002          :8003       :8004
              ↓           ↓           ↓
         [LLM Reasoning :8005]
              ↓
         [Action Execution :8007]
              ↓
         [Verification :8009]
              ↓
   [Memory :8008] [Observability :8010] [Explainability :8011]
              ↓
   [Frontend Dashboard :8501]
```

## Microservices

| Service | Port | Description |
|---------|------|-------------|
| API Gateway | 8000 | Single entry point — auth enforcement, rate limiting, routing, WebSocket live updates |
| Auth | 8001 | User registration, API key issuance (HMAC-SHA256), key rotation, RBAC |
| Screen Capture | 8002 | Full-screen screenshot capture via `mss`, returns base64-encoded PNG |
| Vision | 8003 | OCR via EasyOCR, UI element detection, bounding-box extraction |
| State Builder | 8004 | Aggregates vision output into a structured `ScreenState` object |
| LLM Reasoning | 8005 | GPT-4o / Ollama reasoning — produces next action, confidence, and rationale |
| Task Planner | 8006 | Task lifecycle (PENDING → RUNNING → COMPLETED), step planning, Redis plan cache |
| Action Execution | 8007 | Mouse/keyboard automation via PyAutoGUI with sandboxing and allowlisting |
| Memory | 8008 | Short-term + long-term memory, FAISS vector search, sentence-transformer embeddings |
| Verification | 8009 | Post-action screenshot comparison to confirm expected outcomes |
| Observability | 8010 | Prometheus metrics aggregation, structured log ingestion, step timing |
| Explainability | 8011 | Per-step decision rationale, confidence scores, alternative action logging |
| Task Queue | 8012 | Priority task queue backed by Redis |
| Frontend | 8501 | Streamlit dashboard — live task monitoring, action history, memory browser |

## Tech Stack

| Technology | Purpose |
|------------|---------|
| **FastAPI** | All service HTTP APIs (async, OpenAPI auto-docs) |
| **Python 3.11+** | Primary language across all services |
| **PostgreSQL 13+** | Persistent storage — tasks, actions, users, audit logs |
| **Redis** | Task queue, plan caching, pub/sub for live updates |
| **FAISS** | Vector similarity search for long-term memory retrieval |
| **sentence-transformers** | `all-MiniLM-L6-v2` embeddings for memory encoding |
| **EasyOCR** | On-device OCR — extracts text from screenshots |
| **PyAutoGUI** | Cross-platform mouse/keyboard automation |
| **mss** | Fast multi-screen screenshot capture |
| **OpenAI / Ollama** | LLM provider (GPT-4o cloud or Llama local) |
| **asyncpg** | Async PostgreSQL driver |
| **Prometheus** | Metrics collection and storage |
| **Grafana** | Metrics dashboards |
| **ELK Stack** | Centralised log aggregation (Elasticsearch + Logstash + Kibana) |
| **OpenTelemetry / Jaeger** | Distributed tracing |
| **Streamlit** | Operator dashboard frontend |
| **Docker Compose** | Local multi-service orchestration |
| **Kubernetes + Helm** | Production deployment and auto-scaling |
| **slowapi** | Rate limiting for the API Gateway |
| **Pydantic v2** | Request/response validation and settings management |
| **tenacity** | LLM call retry with exponential back-off |

## Quick Start

### Prerequisites

- Docker 24+ and Docker Compose 2.24+
- Python 3.11+ (for local development only)
- OpenAI API key **or** a running [Ollama](https://ollama.ai/) instance

### 1. Clone and Configure

```bash
git clone https://github.com/yadavanujkumar/Screen-Mind.git
cd Screen-Mind
cp .env.example .env
# Edit .env — set OPENAI_API_KEY (or OLLAMA_URL), POSTGRES_PASSWORD, REDIS_PASSWORD
```

### 2. Start with Docker Compose

```bash
docker-compose up -d
```

All 13 microservices, PostgreSQL, Redis, Prometheus, Grafana, and the ELK stack start automatically.

### 3. Initialize the Database

```bash
docker exec -i screenmind-postgres psql -U screenmind -d screenmind < database/schema.sql
```

### 4. Create Your First User

```bash
curl -X POST http://localhost:8001/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "email": "admin@example.com", "role": "admin"}'
```

The response includes a plaintext `api_key` — **store it securely, it is shown only once**.

### 5. Access the Dashboard

Open **http://localhost:8501** in your browser to reach the Streamlit operator dashboard.

## API Reference

All external API calls go through the API Gateway on port **8000** and require the `X-API-Key` header.

### Submit a Task

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Open the browser and search for weather in New York",
    "user_id": "your-user-uuid",
    "priority": "normal"
  }'
```

Response:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "description": "Open the browser and search for weather in New York",
  "created_at": "2024-01-15T10:30:00Z"
}
```

### Execute a Task

```bash
curl -X POST http://localhost:8000/api/v1/tasks/{task_id}/execute \
  -H "X-API-Key: your-api-key"
```

### Get Task Status

```bash
curl http://localhost:8000/api/v1/tasks/{task_id}/status \
  -H "X-API-Key: your-api-key"
```

### Get Explainability Report

```bash
curl http://localhost:8000/api/v1/tasks/{task_id}/explainability \
  -H "X-API-Key: your-api-key"
```

### Live WebSocket Updates

```javascript
const ws = new WebSocket(
  "ws://localhost:8000/ws/tasks/{task_id}/live?api_key=your-api-key"
);
ws.onmessage = (event) => console.log(JSON.parse(event.data));
```

See [docs/api-reference.md](docs/api-reference.md) for the complete API documentation.

## Agent Loop

Each iteration of the agent loop proceeds through eight phases:

| Phase | Service | Description |
|-------|---------|-------------|
| **Observe** | Screen Capture | Captures a full-resolution screenshot via `mss` |
| **Understand** | Vision + State Builder | EasyOCR extracts all text; element detection identifies buttons, inputs, links |
| **Reason** | LLM Reasoning | GPT-4o / Llama analyses screen state, goal, memory context, and prior actions to pick the best next action |
| **Plan** | Task Planner | Expands the reasoning output into an ordered list of concrete sub-steps; caches plan in Redis |
| **Act** | Action Execution | Executes the chosen action via PyAutoGUI (click, type, key press, scroll, drag, etc.) |
| **Verify** | Verification | Takes a fresh screenshot and compares it against the expected outcome |
| **Log** | Observability | Persists step timing, metrics, and structured events to PostgreSQL and Prometheus |
| **Learn** | Memory | Encodes the step result as a vector embedding and stores it in FAISS for future retrieval |

The loop runs for up to **50 steps** by default (configurable). It exits early when the LLM returns `"task_complete": true`.

## Supported Actions

The Action Execution service implements 13 action types:

| Action | Parameters | Description |
|--------|-----------|-------------|
| `CLICK` | `coordinates: [x, y]` | Single left-click at screen position |
| `DOUBLE_CLICK` | `coordinates: [x, y]` | Double left-click |
| `RIGHT_CLICK` | `coordinates: [x, y]` | Right-click (context menu) |
| `MOVE_MOUSE` | `coordinates: [x, y]` | Move mouse without clicking |
| `TYPE_TEXT` | `text: str` | Type a string character by character |
| `PRESS_KEY` | `key: str` | Press a keyboard key or combination |
| `SCROLL_UP` | `coordinates?: [x, y]` | Scroll up 3 units at position |
| `SCROLL_DOWN` | `coordinates?: [x, y]` | Scroll down 3 units at position |
| `DRAG_AND_DROP` | `coordinates: [x, y]`, `end_coordinates: [x, y]` | Drag from start to end |
| `OPEN_APPLICATION` | `app_name: str` | Launch an allowlisted application |
| `OPEN_WEBSITE` | `url: str` | Open an HTTP/HTTPS URL in the default browser |
| `WAIT` | `seconds: float` | Pause execution (max 60 s) |
| `TAKE_SCREENSHOT` | — | Capture a screenshot via the screen-capture service |

## Security

- **API key authentication** — all gateway requests require `X-API-Key`; keys are stored as HMAC-SHA256 hashes using a server-side secret
- **Role-based access control** — three roles: `admin`, `operator`, `viewer`
- **Action sandboxing** — set `SAFE_MODE=true` to log actions without executing them
- **Dangerous key blocking** — `BLOCK_DANGEROUS_KEYS=true` blocks combinations such as `Ctrl+Alt+Delete`, `Alt+F4`, `Win+L`
- **Application allowlisting** — `OPEN_APPLICATION` only launches processes listed in `ALLOWED_APP_NAMES`
- **URL validation** — `OPEN_WEBSITE` only accepts `http://` and `https://` schemes
- **Rate limiting** — 100 requests / minute per IP (configurable via `RATE_LIMIT`)
- **Audit logs** — every authenticated action is recorded in the `audit_logs` database table

## Observability

| Tool | URL | Purpose |
|------|-----|---------|
| Prometheus | http://localhost:9090 | Metrics scraping and storage |
| Grafana | http://localhost:3000 | Dashboards (login: `admin` / `$GRAFANA_PASSWORD`) |
| Kibana | http://localhost:5601 | Log exploration and search |
| Jaeger | http://localhost:16686 | Distributed trace viewer |

Each service exposes a `/metrics` endpoint in Prometheus exposition format. Key metrics include:

- `api_gateway_requests_total` — total HTTP requests by method, endpoint, status code
- `api_gateway_request_latency_seconds` — p50/p95/p99 latency histograms
- `llm_call_latency_seconds` — LLM response time by provider and model
- `llm_tokens_total` — prompt and completion token usage
- `actions_executed_total` — actions by type and outcome
- `action_execution_latency_seconds` — per-action-type timing
- `tasks_created_total` — task throughput
- `plan_generation_latency_seconds` — planning time

## Kubernetes Deployment

```bash
# Apply manifests in order
kubectl apply -f k8s/manifests/namespace.yaml
kubectl apply -f k8s/manifests/secrets.yaml    # Update with real secrets first
kubectl apply -f k8s/manifests/configmap.yaml
kubectl apply -f k8s/manifests/postgres.yaml
kubectl apply -f k8s/manifests/api-gateway.yaml
kubectl apply -f k8s/manifests/ingress.yaml

# Verify all pods are running
kubectl get pods -n screenmind
```

Each service deployment uses a `HorizontalPodAutoscaler` targeting 70% CPU utilisation. Scale stateless services (API Gateway, LLM Reasoning, Action Execution) horizontally; scale PostgreSQL vertically or switch to a managed cloud database.

## Development

### Run a Single Service Locally

```bash
cd services/llm-reasoning
pip install -r requirements.txt
uvicorn main:app --reload --port 8005
```

### Run Tests

```bash
pip install pytest pytest-asyncio httpx
pytest tests/
```

### Auto-generated API Docs

Each service exposes interactive Swagger UI at `/docs` and ReDoc at `/redoc`. For example:

- API Gateway: http://localhost:8000/docs
- Auth Service: http://localhost:8001/docs
- LLM Reasoning: http://localhost:8005/docs

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://screenmind:...@localhost:5432/screenmind` | PostgreSQL connection string |
| `POSTGRES_PASSWORD` | — | PostgreSQL password (used in docker-compose) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `REDIS_PASSWORD` | — | Redis password |
| `OPENAI_API_KEY` | — | OpenAI API key (required if `LLM_PROVIDER=openai`) |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI chat model name |
| `LLM_PROVIDER` | `openai` | LLM backend: `openai` or `ollama` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL (used if `LLM_PROVIDER=ollama`) |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `SAFE_MODE` | `false` | When `true`, actions are logged but not executed |
| `BLOCK_DANGEROUS_KEYS` | `true` | Block destructive key combinations |
| `ALLOWED_ACTIONS` | _(all)_ | Comma-separated list of permitted action types |
| `ALLOWED_APP_NAMES` | — | Comma-separated allowlist for `OPEN_APPLICATION` |
| `GRAFANA_PASSWORD` | `changeme_grafana_password` | Grafana admin password |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch URL for log shipping |
| `JAEGER_HOST` | `localhost` | Jaeger agent host for distributed tracing |
| `JAEGER_PORT` | `6831` | Jaeger agent UDP port |
| `FAISS_INDEX_PATH` | `/data/faiss.index` | Path for persisting the FAISS vector index |
| `PLAN_CACHE_TTL` | `3600` | Redis TTL (seconds) for cached task plans |
| `API_KEY_SECRET` | `change-me-in-production` | Server-side HMAC secret for API key hashing |

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Screen-Mind is an open-source project. Contributions, bug reports, and feature requests are welcome via GitHub Issues and Pull Requests.*