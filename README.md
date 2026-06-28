# DuckDuckGo Browser MCP Server

A production-ready **Model Context Protocol (MCP)** server that performs **real-time internet searches** via the DuckDuckGo Lite endpoint (`https://lite.duckduckgo.com/lite/`) with automatic retry logic, intelligent caching, and robust error handling across all transport modes.

**Previously:** Simulated search with a deterministic knowledge base  
**Now:** Live web search results with enterprise-grade reliability

---

## What's New

✨ **Real-Time Search**: Fetch live results from DuckDuckGo Lite  
🔄 **Automatic Retries**: 3 attempts with exponential backoff  
💾 **Smart Caching**: 5-minute TTL with stale-data fallback  
⏱️ **Timeout Protection**: Request-level and session-level timeouts  
📊 **Auto-Detection**: Category detection from search results  
🛡️ **Robust Modes**: Enhanced error handling for STDIO, SSE, and Streamable HTTP  
📝 **Detailed Logging**: Debug-friendly operation tracking  

---

## Folder Structure

```
duckduckgo-browser-mcp/
├── src/
│   └── duckduckgo_browser/
│       ├── __init__.py              # Package entry point (sync main())
│       ├── __main__.py              # python -m duckduckgo_browser
│       ├── server.py                # MCP app + all three transport modes
│       ├── services/
│       │   ├── __init__.py
│       │   ├── search_engine.py     # Real-time search via DuckDuckGo Lite
│       │   └── web_scraper.py       # Web scraper with retry logic & caching
│       └── tools/
│           ├── __init__.py
│           ├── toolhandler.py       # Abstract base class for tools
│           └── search_tools.py      # get_internet_result tool handler
├── tests/
│   ├── __init__.py
│   └── test_search_tools.py
├── Dockerfile
├── pyproject.toml
├── pytest.ini
└── README.md
```

---

## Installation

```bash
cd duckduckgo-browser-mcp
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e .
```

**Dependencies:**
- mcp[cli]>=1.12.0
- starlette>=0.27.0
- uvicorn>=0.20.0
- requests>=2.31.0 (HTTP client)
- beautifulsoup4>=4.12.0 (HTML parsing)

---

## Running the Server

### 1. STDIO mode
Used by MCP hosts like Claude Desktop, IDE plugins, and CLI clients.
```bash
python -m duckduckgo_browser --mode stdio
```
The server reads JSON-RPC frames from **stdin** and writes responses to **stdout**.

### 2. SSE mode (Server-Sent Events)
```bash
python -m duckduckgo_browser --mode sse --host 0.0.0.0 --port 7070
```
- `GET  http://localhost:7070/sse`       — open SSE stream
- `POST http://localhost:7070/messages/` — send MCP request frames

### 3. Streamable HTTP mode (Recommended for Production)
```bash
python -m duckduckgo_browser --mode streamable-http --host 0.0.0.0 --port 7070
```
- `POST http://localhost:7070/mcp` — single endpoint, chunked streaming response

---

## Docker

```bash
# Build
docker build -t duckduckgo-browser-mcp .

# Run (streamable-http, default)
docker run -p 7070:7070 duckduckgo-browser-mcp

# Run SSE mode
docker run -p 7070:7070 duckduckgo-browser-mcp --mode sse --port 7070

# Run STDIO mode (pipe-based)
docker run -i duckduckgo-browser-mcp --mode stdio
```

---

## Tool: `get_internet_result`

Performs real-time searches on DuckDuckGo Lite with automatic retry and caching.

| Field | Type | Required | Description |
|---|---|---|---|
| `input_value` | string | ✅ | Natural language query or search term |

### Example output
```
# DuckDuckGo Browser -- Search Results
- **Query:** "what is machine learning"
- **Topic category detected:** ai
- **Results found:** 5

## Top Results
### 1. Machine Learning Explained
- **URL:** https://www.ibm.com/topics/machine-learning
- **Summary:** Machine learning is a subset of AI ...
- **Relevance score:** 5

## Suggested Websites to Explore
### Hugging Face
- **URL:** https://huggingface.co
- **Why visit:** Leading hub for open-source AI models and datasets

## What to Look For
When researching AI topics, prioritise peer-reviewed papers (arXiv, NeurIPS) 
and official model documentation. Cross-check benchmark claims on Papers With Code.

---
*Results powered by DuckDuckGo Lite (https://lite.duckduckgo.com/lite/).*
*Search performed in real-time with automatic retry and caching for reliability.*
```

---

## Example MCP Client Calls

### Python (stdio)
```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="python",
        args=["-m", "duckduckgo_browser", "--mode", "stdio"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get_internet_result",
                {"input_value": "what is machine learning"},
            )
            print(result.content[0].text)

asyncio.run(main())
```

### Python (SSE)
```python
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    async with sse_client("http://localhost:7070/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get_internet_result",
                {"input_value": "best travel destinations Europe"},
            )
            print(result.content[0].text)

asyncio.run(main())
```

### Python (Streamable HTTP)
```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:7070/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get_internet_result",
                {"input_value": "how does quantum computing work"},
            )
            print(result.content[0].text)

asyncio.run(main())
```

### curl (Streamable HTTP — initialize)
```bash
curl -X POST http://localhost:7070/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl-client","version":"1.0"}}}'
```

### curl (Streamable HTTP — call tool)
```bash
# Replace <SESSION_ID> with the mcp-session-id from the initialize response header
curl -X POST http://localhost:7070/mcp \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: <SESSION_ID>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_internet_result","arguments":{"input_value":"python web frameworks"}}}'
```

---

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest
```

---

## How It Works

### Search Flow

```
User Query
    ↓
get_internet_result tool (async)
    ↓
RealSearchEngine.search()
    ↓
DuckDuckGoScraper.search_with_retry()
    ├─ Check in-memory cache
    ├─ If cached & valid → return cached results
    ├─ If cache miss → fetch from DuckDuckGo Lite
    │   ├─ Attempt 1: Request + parse (10s timeout)
    │   ├─ Timeout? → wait 2s, retry
    │   ├─ Attempt 2: Request + parse
    │   ├─ Timeout? → wait 4s, retry
    │   ├─ Attempt 3: Request + parse
    │   ├─ All fail? → return stale cache or empty
    └─ Cache new results (5 min TTL)
    ↓
Auto-detect Category
    ↓
Curated Website Suggestions
    ↓
Formatted Markdown Response
```

### Key Features

**Retry Logic**
- Maximum 3 attempts per query
- Exponential backoff: 2s, 4s between retries
- Timeout per request: 10 seconds

**Caching**
- In-memory cache with 5-minute TTL
- Prevents repeated web calls for same query
- Stale cache returned on network failures
- Automatic eviction of expired entries

**Error Handling**
- Timeout errors trigger retry logic
- Connection errors check cache
- Network failures return cached data or graceful error message
- STDIO: Catches BrokenPipeError on client disconnect
- SSE: Per-session timeout (1 hour)
- Streamable HTTP: Per-request timeout (60 seconds)

**Category Detection**
Results are automatically categorized based on content patterns:
- AI, programming, cloud, finance, health, science, education, travel, food, sports, general

Each category includes curated website suggestions.

---

## Configuration

Adjust scraper behavior in `src/duckduckgo_browser/services/web_scraper.py`:

```python
DuckDuckGoScraper(
    timeout=10.0,              # Request timeout (seconds)
    max_retries=3,             # Retry attempts
    retry_backoff_base=2.0,    # Exponential backoff base
    cache_ttl=300,             # Cache TTL (seconds)
)
```

Adjust HTTP server timeouts in `src/duckduckgo_browser/server.py`:
- STDIO: Standard input timeout
- SSE: 3600s session timeout
- Streamable HTTP: 60s request timeout

---

## Logging

Server provides detailed logging at multiple levels:

**Startup Output**
```
2026-04-01T15:36:13 INFO  duckduckgo-browser ================================================================================
2026-04-01T15:36:13 INFO  duckduckgo-browser Starting DuckDuckGo Browser MCP Server
2026-04-01T15:36:13 INFO  duckduckgo-browser Python: 3.11.6
2026-04-01T15:36:13 INFO  duckduckgo-browser Mode: streamable-http
2026-04-01T15:36:13 INFO  duckduckgo-browser Listening at http://0.0.0.0:7070
2026-04-01T15:36:13 INFO  duckduckgo-browser Registered tools: ['get_internet_result']
```

**Log Levels**
- **DEBUG**: Cache hits/misses, individual retry attempts, parser details
- **INFO**: Tool registration, server startup, search requests
- **WARNING**: Retry attempts, network failures, cache misses
- **ERROR**: Fatal errors, unrecoverable failures

Enable debug logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## Robustness Summary

| Feature | Details |
|---------|---------|
| **Network Retry** | 3x with exponential backoff (2s, 4s) |
| **Request Timeout** | 10 seconds per attempt |
| **Session Timeout** | 60s HTTP, 1h SSE, unlimited STDIO |
| **Caching** | 5-min TTL + stale fallback |
| **Error Recovery** | Graceful degradation, no crashes |
| **STDIO Mode** | Handles BrokenPipeError, client disconnect |
| **SSE Mode** | Connection error handling, CORS support |
| **Streamable HTTP** | Error responses (504 timeout, 500 error) |
| **Logging** | DEBUG/INFO/WARNING/ERROR levels |

---

## Known Limitations

1. **DuckDuckGo Lite Parse**: Assumes standard DuckDuckGo Lite HTML format
   - May need updates if DDG changes HTML structure
   - Parser handles missing/malformed results gracefully

2. **Network Dependency**: Requires internet connectivity
   - Uses cache to survive brief outages
   - Falls back to empty results after 3 retries

3. **Rate Limiting**: DuckDuckGo may rate-limit rapid requests
   - Exponential backoff helps mitigate
   - Cache reduces repeated queries

4. **In-Memory Cache**: Lost on server restart
   - Suitable for most deployments
   - Consider persistent cache (Redis/SQLite) for high-volume use

---

## Troubleshooting

**Port Already in Use**
```bash
python -m duckduckgo_browser --mode streamable-http --port 8000
```

**Connection Timeout**
- Verify internet connectivity
- Test: `curl https://lite.duckduckgo.com/lite/?q=test`
- Check firewall rules

**No Results**
- Verify query is valid
- Check logs: Enable DEBUG level logging
- Test with simpler query terms

**Dependency Issues**
```bash
pip install -r requirements.txt
# or
pip install -e .
```
