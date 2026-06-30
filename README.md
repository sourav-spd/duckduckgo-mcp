# DuckDuckGo MCP Server

A production-ready **Model Context Protocol (MCP)** server that performs real-time internet searches via the DuckDuckGo Lite endpoint (`https://lite.duckduckgo.com/lite/`) with automatic retry logic, intelligent caching, and robust error handling.

Supports all three MCP transports:
- **stdio** — for MCP desktop hosts and IDE plugins
- **SSE** — Server-Sent Events over HTTP
- **streamable-http** — chunked HTTP streaming (default for container deployment)

---

## Folder Structure

```text
duckduckgo-tool/
├── duckduckgo_server.py             # Main MCP server entry point
├── pyproject.toml
├── Dockerfile
├── README.md
├── pytest.ini
├── tests/
│   ├── __init__.py
│   └── test_search_tools.py
└── src/
    └── duckduckgo_browser/
        ├── __init__.py
        ├── __main__.py              # python -m duckduckgo_browser
        ├── duckduckgo_server.py     # stub (canonical server is at root)
        ├── services/
        │   ├── __init__.py
        │   ├── search_engine.py     # RealSearchEngine — DuckDuckGo Lite
        │   └── web_scraper.py       # DuckDuckGoScraper with retry & cache
        └── tools/
            ├── __init__.py
            ├── toolhandler.py       # Abstract base class
            └── search_tools.py     # get_internet_result tool handler
```

---

## Available Tool (1)

### `get_internet_result`

Performs a real-time search on DuckDuckGo Lite and returns a concise answer with source links.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `input_value` | string | ✅ | Natural language query or search term |

**Example output:**
```
Answer: Machine learning is a branch of artificial intelligence that enables systems to learn
        and improve from experience without being explicitly programmed.
Source 1: https://www.ibm.com/topics/machine-learning
Source 2: https://www.google.com/search?q=what+is+machine+learning
```

---

## Local Setup

```bash
# Create and activate virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -e .
```

**Dependencies:**
- `mcp[cli] >= 1.12.0`
- `starlette >= 0.27.0`
- `uvicorn >= 0.20.0`
- `requests >= 2.31.0`
- `beautifulsoup4 >= 4.12.0`
- `truststore >= 0.10.0`

---

## Run

The server is controlled by environment variables or CLI flags. Environment variables take priority and are used for container deployments.

| Environment Variable | Default | Description |
|---|---|---|
| `TRANSPORT_TYPE` | `streamable-http` | Transport mode: `stdio`, `sse`, `streamable-http` |
| `APP_HOST` | `0.0.0.0` | Bind host |
| `APP_PORT` | `8000` | Bind port |

### stdio (default for MCP desktop hosts)

```bash
duckduckgo-mcp --mode stdio
```

### SSE

```bash
duckduckgo-mcp --mode sse --host 0.0.0.0 --port 8000
```

Endpoints:
- `GET  /sse`        — open SSE stream
- `POST /messages/`  — send MCP request frames
- `GET  /health`     — health check
- `GET  /healthz`    — health check (alias)
- `GET  /`           — server info

### Streamable HTTP

```bash
duckduckgo-mcp --mode streamable-http --host 0.0.0.0 --port 8000
```

Endpoints:
- `POST /mcp`     — single MCP endpoint, chunked streaming response
- `GET  /health`  — health check
- `GET  /healthz` — health check (alias)
- `GET  /`        — server info

---

## Docker

```bash
# Build
docker build -t duckduckgo-mcp .

# Run streamable-http (default)
docker run -p 8000:8000 duckduckgo-mcp

# Run SSE mode
docker run -e TRANSPORT_TYPE=sse -e APP_PORT=8000 -p 8000:8000 duckduckgo-mcp

# Run with custom port
docker run -e TRANSPORT_TYPE=streamable-http -e APP_PORT=9000 -p 9000:9000 duckduckgo-mcp

# Run stdio mode (pipe-based)
docker run -i -e TRANSPORT_TYPE=stdio duckduckgo-mcp
```

---

## MCP Client Configuration

### Streamable HTTP

```json
{
  "mcpServers": {
    "duckduckgo": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

### SSE

```json
{
  "mcpServers": {
    "duckduckgo": {
      "type": "sse",
      "url": "http://localhost:8000/sse"
    }
  }
}
```

### stdio

```json
{
  "mcpServers": {
    "duckduckgo": {
      "command": "duckduckgo-mcp",
      "args": ["--mode", "stdio"]
    }
  }
}
```

---

## Testing with MCP Inspector

**Streamable HTTP:**
1. Start the server: `duckduckgo-mcp --mode streamable-http --port 8000`
2. Open MCP Inspector and connect to: `http://localhost:8000/mcp`
3. Call `get_internet_result` with `{"input_value": "what is machine learning"}`

**SSE:**
1. Start the server: `duckduckgo-mcp --mode sse --port 8000`
2. Open MCP Inspector and connect to: `http://localhost:8000/sse`
3. Call `get_internet_result` with `{"input_value": "best cloud providers 2026"}`

---

## How It Works

### Search Flow

```
User Query
    ↓
get_internet_result (async tool handler)
    ↓
RealSearchEngine.search()
    ↓
DuckDuckGoScraper.search_with_retry()
    ├─ Check in-memory cache (5-min TTL)
    ├─ Cache hit  → return cached results
    └─ Cache miss → fetch from DuckDuckGo Lite
        ├─ Attempt 1 (10s timeout)
        ├─ Fail → wait 2s → Attempt 2
        ├─ Fail → wait 4s → Attempt 3
        └─ All fail → return stale cache or empty result
    ↓
Auto-detect category from result content
    ↓
Format: Answer + Source 1 + Source 2
```

### Key Features

| Feature | Details |
|---|---|
| **Search Source** | DuckDuckGo Lite (`https://lite.duckduckgo.com/lite/`) with HTML + Instant Answer API fallback |
| **Retry Logic** | 3 attempts with exponential backoff (2s, 4s) |
| **Request Timeout** | 10 seconds per attempt |
| **Caching** | In-memory, 5-min TTL, stale fallback on network failure |
| **SSL** | OS trust store via `truststore`; falls back to SSL-disabled as last resort |
| **CORS** | Fully open (`allow_origins=["*"]`) for all HTTP modes |
| **Session Timeout** | 60s per request (streamable-http), unlimited (stdio) |

### Category Detection

Results are automatically categorised from URL, title, and snippet content:

`ai` · `programming` · `cloud` · `finance` · `health` · `science` · `education` · `travel` · `food` · `sports` · `general`

---

## Kubernetes Deployment

For EC2/Kubernetes, set transport mode and port via environment variables — no image rebuild needed:

```yaml
# Streamable HTTP deployment
env:
  - name: TRANSPORT_TYPE
    value: "streamable-http"
  - name: APP_PORT
    value: "8000"
  - name: APP_HOST
    value: "0.0.0.0"

# SSE deployment
env:
  - name: TRANSPORT_TYPE
    value: "sse"
  - name: APP_PORT
    value: "8000"
  - name: APP_HOST
    value: "0.0.0.0"
```

> No supergateway wrapper is needed. The server handles its own HTTP binding directly for both SSE and streamable-http modes.

---

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest
```

---

## Troubleshooting

**Port already in use**
```bash
duckduckgo-mcp --mode streamable-http --port 8001
```

**No search results returned**
- Verify internet connectivity from the host/container
- Test: `curl "https://lite.duckduckgo.com/lite/?q=test"`
- Enable debug logging:
  ```bash
  PYTHONPATH=src python -c "import logging; logging.basicConfig(level=logging.DEBUG)"
  ```

**SSL errors in corporate network**
- The scraper automatically retries with SSL verification disabled as a last resort
- Alternatively, set `verify_ssl=False` in `web_scraper.py` `DuckDuckGoScraper` init

**Import errors after install**
```bash
pip install -e . --force-reinstall
```

---

## Configuration Reference

Tune scraper behaviour in `src/duckduckgo_browser/services/web_scraper.py`:

```python
DuckDuckGoScraper(
    timeout=10.0,           # Request timeout per attempt (seconds)
    max_retries=3,          # Number of retry attempts
    retry_backoff_base=2.0, # Exponential backoff base (2s, 4s, ...)
    cache_ttl=300,          # Cache TTL in seconds (5 minutes)
)
```

---

## Robustness Summary

| Scenario | Behaviour |
|---|---|
| Network timeout | Retry up to 3x with exponential backoff |
| All retries fail | Return stale cache if available, else empty result |
| DuckDuckGo Lite unavailable | Fall back to HTML endpoint, then Instant Answer API |
| SSL certificate error | Retry with SSL verification disabled |
| Invalid query | Validation error returned as TextContent |
| Port conflict | Clear error log with suggested fix |
| Container restart | Cache cleared (in-memory); fresh searches on next request |

---

## Requirements

- Python 3.10+
- Internet access (for DuckDuckGo searches)
- No API key required

## License

MIT License — see LICENSE file for details
