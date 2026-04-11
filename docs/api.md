# API Reference

Sovyx exposes a REST API and WebSocket through the built-in dashboard server, powered by FastAPI.

## Authentication

All API endpoints require a bearer token. The token is auto-generated at startup and displayed in the logs, or can be set explicitly:

```yaml
# mind.yaml
dashboard:
  token: "your-secret-token"
```

Pass it via header:

```
Authorization: Bearer <token>
```

## Endpoints

### Status

#### `GET /api/status`

Returns the current engine status, including uptime, active minds, and resource usage.

**Response:**

```json
{
  "status": "running",
  "uptime_seconds": 3600,
  "minds_active": 1,
  "version": "0.5.0"
}
```

### Health

#### `GET /api/health`

Health check endpoint for monitoring and load balancers.

**Response:**

```json
{
  "healthy": true,
  "database": "ok",
  "llm": "ok"
}
```

### Conversations

#### `GET /api/conversations`

List recent conversations across all minds.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | Max conversations to return |
| `offset` | int | 0 | Pagination offset |

**Response:**

```json
[
  {
    "id": "conv_abc123",
    "mind_id": "aria",
    "person": "Alice",
    "messages": 42,
    "last_active": "2026-04-07T05:00:00Z"
  }
]
```

#### `GET /api/conversations/{conversation_id}`

Get full conversation history with metadata.

### Brain

#### `GET /api/brain/graph`

Returns the brain's concept graph for visualization. Includes concepts as nodes and relations as edges with weights.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mind_id` | str | required | Target mind |
| `limit` | int | 100 | Max nodes to return |

**Response:**

```json
{
  "nodes": [
    {"id": "c_1", "name": "Python", "type": "concept", "weight": 0.85}
  ],
  "edges": [
    {"source": "c_1", "target": "c_2", "weight": 0.72}
  ]
}
```

#### `GET /api/brain/search`

Semantic search over the brain's concept store.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | str | required | Search query |
| `mind_id` | str | required | Target mind |
| `limit` | int | 10 | Max results |

**Response:**

```json
[
  {
    "concept_id": "c_42",
    "name": "machine learning",
    "score": 0.92,
    "summary": "..."
  }
]
```

### Logs

#### `GET /api/logs`

Stream recent structured log entries.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `level` | str | `"INFO"` | Minimum log level |
| `limit` | int | 100 | Max entries |

### Settings

#### `GET /api/settings`

Get current mind settings (personality, LLM config, budgets).

#### `PUT /api/settings`

Update mind settings at runtime. Changes are persisted to `mind.yaml`.

**Body:**

```json
{
  "personality": {
    "tone": "warm",
    "humor": 0.6
  },
  "llm": {
    "budget_daily_usd": 5.0
  }
}
```

### Configuration

#### `GET /api/config`

Get full system + mind configuration as JSON.

#### `PUT /api/config`

Update configuration. Validates against schema before applying.

## WebSocket

### `WS /ws`

Real-time event stream for the dashboard.

**Connection:**

```javascript
const ws = new WebSocket("ws://localhost:8765/ws?token=<token>");

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(data.type, data.payload);
};
```

**Event Types:**

| Type | Description |
|------|-------------|
| `message.received` | New message from a channel |
| `message.sent` | Response sent to channel |
| `brain.concept_created` | New concept learned |
| `brain.consolidation` | Brain consolidation cycle |
| `llm.request` | LLM API call made |
| `llm.cost` | Cost tracking update |
| `error` | Error event |

## Metrics (v0.5+)

### `GET /metrics`

Prometheus-compatible metrics endpoint (no authentication required).

**Format:** OpenMetrics text

**Available Metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `sovyx_requests_total` | counter | Total API requests |
| `sovyx_request_duration_seconds` | histogram | Request latency |
| `sovyx_brain_concepts_total` | gauge | Total concepts in brain |
| `sovyx_llm_cost_usd_total` | counter | Cumulative LLM cost |
| `sovyx_active_minds` | gauge | Currently active minds |

---

## v0.5 Endpoints

### Backup

#### `POST /api/backup`

Create a manual backup of the database.

**Response:**
```json
{
  "path": "/home/user/.sovyx/backups/backup_manual_20260407.db",
  "trigger": "manual",
  "size_bytes": 524288,
  "encrypted": false,
  "created_at": "2026-04-07T17:00:00Z"
}
```

### License

#### `GET /api/license`

Get current license status.

**Response:**
```json
{
  "status": "valid",
  "tier": "pro",
  "expires_in_days": 25,
  "grace_days_remaining": 0,
  "can_operate": true
}
```

### Upgrade

#### `GET /api/upgrade/check`

Check if an upgrade is available.

**Response:**
```json
{
  "current_version": "0.5.0",
  "has_pending": true,
  "pending_count": 2,
  "migrations": [
    {"version": "0.6.0", "description": "Add voice tables"}
  ]
}
```

#### `POST /api/upgrade`

Start the upgrade process. Returns the upgrade report.

### Doctor

#### `GET /api/doctor`

Run all diagnostic checks and return the report.

**Response:**
```json
{
  "healthy": true,
  "passed": 11,
  "warned": 0,
  "failed": 0,
  "results": [
    {"check": "db_integrity", "status": "pass", "message": "OK", "duration_ms": 12}
  ]
}
```

### Plugins

#### `GET /api/plugins`

List all installed plugins with enriched metadata.

**Response:**
```json
[
  {
    "name": "weather",
    "version": "1.0.0",
    "description": "Weather forecasts via Open-Meteo.",
    "enabled": true,
    "status": "active",
    "tools": [
      {"name": "get_weather", "description": "Get current weather for a city."}
    ],
    "permissions": ["network:internet"],
    "category": "utilities",
    "author": "Sovyx",
    "icon_url": null,
    "pricing": "free"
  }
]
```

#### `GET /api/plugins/{name}`

Get detailed information about a specific plugin.

#### `POST /api/plugins/{name}/enable`

Enable a plugin. Requires prior permission approval.

#### `POST /api/plugins/{name}/disable`

Disable a plugin. Running tool calls are allowed to complete.

#### `DELETE /api/plugins/{name}`

Remove a plugin. Requires confirmation.
