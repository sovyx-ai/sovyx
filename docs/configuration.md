# Configuration Reference

## System Configuration (`~/.sovyx/system.yaml`)

Global settings for the Sovyx daemon.

### Database

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `database.data_dir` | Path | `~/.sovyx` | Data directory |
| `database.wal_mode` | bool | `true` | SQLite WAL mode |
| `database.read_pool_size` | int | `3` | Read connection pool size |

### Telemetry

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `telemetry.enabled` | bool | `false` | Enable telemetry |

## Mind Configuration (`~/.sovyx/<mind>/mind.yaml`)

Per-mind settings.

### Core

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | str | required | Mind name |
| `language` | str | `"en"` | Primary language |

### Personality (OCEAN Model)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `personality.openness` | float | `0.7` | 0.0–1.0 | Creativity, curiosity |
| `personality.conscientiousness` | float | `0.8` | 0.0–1.0 | Organization, reliability |
| `personality.extraversion` | float | `0.5` | 0.0–1.0 | Sociability, energy |
| `personality.agreeableness` | float | `0.7` | 0.0–1.0 | Cooperation, empathy |
| `personality.neuroticism` | float | `0.3` | 0.0–1.0 | Emotional instability |

### Brain

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `brain.consolidation_interval_hours` | int | `6` | Hours between consolidation cycles |

### LLM

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `llm.default_model` | str | `claude-sonnet-4-20250514` | Primary model |
| `llm.fast_model` | str | `claude-3-5-haiku-20241022` | Fast model for simple queries |
| `llm.local_model` | str | `llama3.2:1b` | Local Ollama model |

## Environment Variables

All environment variables use the `SOVYX_` prefix.

| Variable | Description |
|----------|-------------|
| `SOVYX_ANTHROPIC_API_KEY` | Anthropic API key |
| `SOVYX_OPENAI_API_KEY` | OpenAI API key |
| `SOVYX_TELEGRAM_TOKEN` | Telegram bot token |
| `SOVYX_DATA_DIR` | Override data directory |
| `SOVYX_LOG_LEVEL` | Log level (DEBUG, INFO, WARNING, ERROR) |
