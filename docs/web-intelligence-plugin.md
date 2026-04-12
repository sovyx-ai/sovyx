# Web Intelligence Plugin

Enterprise-grade web search, content extraction, research synthesis, and brain integration.

**Zero API key required** — works out of the box with DuckDuckGo. Optional: SearXNG (self-hosted), Brave Search (API key).

## Architecture

```
                        ┌─────────────────────┐
                        │   LLM / ReAct Loop   │
                        └──────────┬──────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │   WebIntelligencePlugin      │
                    │   6 tools · 3 backends       │
                    │   credibility · cache · brain│
                    └──────────────┬──────────────┘
                                   │
        ┌──────────┬───────────────┼───────────────┬──────────┐
        ▼          ▼               ▼               ▼          ▼
   ┌────────┐ ┌────────┐   ┌───────────┐   ┌──────────┐ ┌────────┐
   │ search │ │ fetch  │   │ research  │   │  lookup  │ │ brain  │
   │        │ │        │   │           │   │          │ │ learn/ │
   │ web    │ │ extract│   │ multi-step│   │ define   │ │ recall │
   │ news   │ │ trafil.│   │ citations │   │ price    │ │        │
   │ auto   │ │ meta   │   │ credibil. │   │ convert  │ │ prov.  │
   └───┬────┘ └───┬────┘   └─────┬─────┘   │ weather  │ └───┬────┘
       │          │               │         └─────┬────┘     │
       ▼          ▼               ▼               ▼          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                    Search Backends                          │
   │  DuckDuckGo (default)  │  SearXNG  │  Brave Search         │
   │  zero API key          │  self-host│  API key               │
   └─────────────────────────────────────────────────────────────┘
```

## Tools

### `search` — Web & News Search

Search the web with automatic intent classification.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Search query |
| `mode` | string | `"auto"` | `auto`, `web`, or `news` |
| `max_results` | int | 5 | Results to return (1-20) |

**Auto mode** classifies intent:
- **Factual** → web search (e.g., "what is Python")
- **Temporal** → news search (e.g., "breaking news today")
- **Price** → news search (e.g., "Bitcoin price")
- **Procedural** → web search (e.g., "how to install Docker")

Each result includes **credibility scoring** (tier1/tier2/tier3/unknown).

### `fetch` — Content Extraction

Fetch and extract readable content from a URL.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | string | required | HTTP(S) URL |
| `max_chars` | int | 4000 | Max characters (100-50000) |

Uses **trafilatura** for best-in-class extraction: title, author, date, language, site name. Fallback to regex when trafilatura unavailable.

**Safety:** SSRF protection blocks private IPs, localhost, file://, ftp://.

### `research` — Multi-Step Synthesis

Deep research with citations and credibility ranking.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Research question |
| `max_sources` | int | 3 | Sources to fetch (1-5) |
| `include_news` | bool | true | Also search news |

**Pipeline:**
1. Search web + news (deduplicated by URL)
2. Rank by credibility score (tier1 first)
3. Fetch top N sources via trafilatura
4. Build numbered citation map `[1] [2] [3]`
5. Return organized findings with avg credibility

### `lookup` — Quick Answers

Instant answers for definitions, prices, conversions, weather.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Lookup query |
| `mode` | string | `"auto"` | `auto`, `define`, `convert`, `price`, `weather` |

Auto-detection keywords:
- **Price:** bitcoin, stock, preço, cotação
- **Weather:** weather, temperatura, forecast, chuva
- **Convert:** "X to Y", "para", converter
- **Define:** "what is", "o que é", define

Weather mode uses **Open-Meteo API** (same as WeatherPlugin).

### `learn_from_web` — Save to Brain

Save web findings to the brain with source provenance.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | required | Concept name |
| `content` | string | required | Content to save |
| `url` | string | `""` | Source URL |
| `author` | string | `""` | Source author |
| `date` | string | `""` | Publication date |
| `category` | string | `"web_research"` | Brain category |

Confidence derived from credibility: tier1 → 0.9, tier2 → 0.8, unknown → 0.5.

Provenance metadata includes: URL, author, date, credibility score, retrieval timestamp.

### `recall_web` — Search Brain

Search brain for previously learned web findings.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Search query |
| `max_results` | int | 5 | Max results (1-20) |

## Search Backends

| Backend | API Key | Features | Best For |
|---------|---------|----------|----------|
| **DuckDuckGo** | None | text, news | Default, zero setup |
| **SearXNG** | None | 70+ engines, self-hosted | Privacy, customization |
| **Brave Search** | Required | web, news, high quality | Production, rate limits |

## Source Credibility

Three-tier domain reputation system:

| Tier | Score | Examples |
|------|-------|---------|
| **Tier 1** (0.95) | Academic, Government, Wire | arxiv.org, reuters.com, gov.br, who.int |
| **Tier 2** (0.80) | News, Tech, Reference | bbc.com, bloomberg.com, github.com, wikipedia.org |
| **Tier 3** (0.50) | Social, UGC | reddit.com, medium.com, twitter.com |
| **Unknown** (0.30-0.55) | TLD heuristics | .edu → 0.80, .xyz → 0.30, HTTPS +0.05 |

Subdomain matching: `news.reuters.com` → tier1 (0.90).

## Intelligent Cache

Intent-adaptive TTL:

| Intent | TTL | Rationale |
|--------|-----|-----------|
| Price | 5 min | Volatile data |
| Temporal | 10 min | Recent events |
| Default | 30 min | General queries |
| Factual | 1 hour | Stable facts |
| Procedural | 24 hours | How-to rarely changes |

Features: case-insensitive keys, LRU eviction, max 200 entries, hit rate stats.

## Configuration

```yaml
plugins:
  web-intelligence:
    backend: duckduckgo          # duckduckgo | searxng | brave
    searxng_url: ""              # Required if backend=searxng
    brave_api_key: ""            # Required if backend=brave
    default_max_results: 5       # 1-20
    fetch_max_chars: 4000        # 100-50000
    cache_enabled: true
    cache_max_size: 200          # 10-1000
    auto_learn: false            # Auto-save research to brain
    timeouts:
      search: 10                 # seconds
      fetch: 15
      research: 60
```

## Rate Limits

| Operation | Limit | Window |
|-----------|-------|--------|
| Search | 30/min | 60s sliding |
| Fetch | 20/min | 60s sliding |
| Research | 5/min | 60s sliding |

## Safety

- **SSRF Protection:** Blocks private IPs (10.x, 172.16-31.x, 192.168.x, 127.x), localhost, file://, ftp://, javascript:
- **Query Sanitization:** Strips control characters, normalizes whitespace
- **Content Size Limit:** Max 1MB HTML response
- **Redirect Limit:** Max 5 redirects
- **Timeout Hierarchy:** search 10s < fetch 15s < research 60s

## Dependencies

```toml
[project.optional-dependencies]
search = ["ddgs>=9.0", "trafilatura>=2.0"]
```

Both optional — plugin degrades gracefully:
- Without `ddgs`: search unavailable
- Without `trafilatura`: fetch uses regex fallback

## Permissions

```
network:internet    # Required for all tools
brain:read          # For recall_web
brain:write         # For learn_from_web
```

## Testing

- **200 unit tests** — all tools, all backends, all edge cases
- **24 property tests** — Hypothesis invariant verification
- **Coverage:** >95% per file
