# Plugins

!!! note "v1.0 Feature"
    The full plugin system ships in Sovyx v1.0. The framework and extension points exist in v0.5.

Sovyx supports plugins to extend the cognitive loop with custom tools, actions, and integrations.

## Architecture

Plugins hook into the **Act** phase of the cognitive loop (Perceive → Attend → Think → **Act** → Reflect). When the LLM generates a tool call, the plugin framework routes it to the appropriate handler.

```
LLM Response → Tool Call Detected → Plugin Router → Handler → Result → LLM
```

The plugin system uses a ReAct (Reasoning + Acting) pattern with a configurable maximum depth to prevent infinite tool loops.

## Plugin Interface

Plugins implement the tool handler protocol:

```python
from sovyx.engine.protocols import BrainReader

class MyPlugin:
    """Example plugin that searches the web."""

    name = "web_search"
    description = "Search the web for current information"

    async def execute(
        self,
        parameters: dict,
        brain: BrainReader,
    ) -> str:
        """Execute the tool and return a text result."""
        query = parameters["query"]
        # ... perform search ...
        return f"Results for: {query}"
```

## Registration

Register plugins in `mind.yaml`:

```yaml
plugins:
  enabled:
    - web_search
    - calculator
    - home_assistant

  web_search:
    api_key_env: SEARCH_API_KEY
    max_results: 5

  home_assistant:
    url: http://homeassistant.local:8123
    token_env: HASS_TOKEN
```

Or programmatically:

```python
from sovyx.cognitive.act import ToolExecutor

executor = ToolExecutor(max_depth=3)
executor.register_tool("web_search", MyPlugin())
```

## Built-in Plugins (Planned)

| Plugin | Version | Description |
|--------|---------|-------------|
| `calculator` | v1.0 | Mathematical expressions |
| `web_search` | v1.0 | Web search via Brave/Google |
| `home_assistant` | v1.0 | Smart home control |
| `calendar` | v1.0 | Google/CalDAV calendar access |
| `notes` | v1.0 | Persistent note-taking |
| `finance` | v1.2 | Portfolio tracking, market data |

## Brain Access

Plugins receive a read-only brain interface (`BrainReader`) for context-aware behavior:

```python
async def execute(self, parameters: dict, brain: BrainReader) -> str:
    # Search the mind's memory
    results = await brain.search("user preferences", mind_id=self.mind_id)

    # Get related concepts
    related = await brain.get_related(concept_id="c_42", limit=5)

    # Full recall (concepts + episodes)
    concepts, episodes = await brain.recall("meeting notes", mind_id=self.mind_id)
```

Available `BrainReader` methods:

| Method | Description |
|--------|-------------|
| `search(query, mind_id, limit)` | Semantic concept search |
| `get_concept(concept_id)` | Get concept by ID |
| `recall(query, mind_id)` | Full recall (concepts + episodes) |
| `get_related(concept_id, limit)` | Get related concepts |

## Error Handling

Plugin errors are isolated — a crashing plugin doesn't take down the engine:

| Exception | Behavior |
|-----------|----------|
| `PluginError` | Logged, error result returned to LLM |
| `PluginLoadError` | Plugin skipped at startup, warning logged |
| `PluginCrashError` | Plugin disabled after 3 consecutive failures |

## Creating a Plugin Package

Plugins can be distributed as Python packages:

```
sovyx-plugin-weather/
├── pyproject.toml
├── src/
│   └── sovyx_plugin_weather/
│       ├── __init__.py
│       └── plugin.py
```

```toml
# pyproject.toml
[project]
name = "sovyx-plugin-weather"
dependencies = ["sovyx>=0.5"]

[project.entry-points."sovyx.plugins"]
weather = "sovyx_plugin_weather.plugin:WeatherPlugin"
```

Sovyx discovers plugins via entry points at startup.
