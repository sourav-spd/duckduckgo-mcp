"""
DuckDuckGo Browser MCP Server
==============================
Supports three transport modes selectable via --mode:

STDIO mode
----------
  The host process (e.g. Claude Desktop, an MCP client) communicates with this
  server over stdin/stdout using newline-delimited JSON-RPC 2.0 frames.
  - No network port is opened.
  - The server reads one MCP request at a time from stdin, processes it, and
    writes the response to stdout.
  - Ideal for local tool integrations and IDE plugins.
  Run: python -m duckduckgo_browser --mode stdio

SSE mode (Server-Sent Events)
------------------------------
  Two HTTP endpoints are exposed:
    GET  /sse        — client opens a long-lived SSE connection; the server
                       pushes MCP response frames as `data:` events.
    POST /messages/  — client sends MCP request frames as HTTP POST bodies.
  - Each GET /sse call creates a paired (read_stream, write_stream) and runs
    the MCP app.run() coroutine for that session.
  - Suitable for web-based MCP clients and browser integrations.
  Run: python -m duckduckgo_browser --mode sse --port 7070

Streamable HTTP mode
--------------------
  Single endpoint:
    POST /mcp  — accepts MCP request frames; responds with chunked HTTP
                 transfer encoding so the client can read incremental frames
                 as they are produced (json_response=False enables streaming).
  - StreamableHTTPSessionManager tracks session IDs via the `mcp-session-id`
    response header so stateful multi-turn conversations work correctly.
  - Best for production deployments behind a load balancer or API gateway.
  Run: python -m duckduckgo_browser --mode streamable-http --port 7070
"""

from __future__ import annotations

import os
import argparse
import asyncio
import contextlib
import logging
import sys
import traceback
from collections.abc import AsyncIterator, Sequence
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

from duckduckgo_browser.tools import GetInternetResultToolHandler, ToolHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("duckduckgo-browser")

# Core MCP application instance
app = Server("duckduckgo-browser-mcp")
_tool_handlers: dict[str, ToolHandler] = {}


def _add_tool_handler(handler: ToolHandler) -> None:
    _tool_handlers[handler.name] = handler


def register_all_tools() -> None:
    _add_tool_handler(GetInternetResultToolHandler())
    logger.info("Registered tools: %s", list(_tool_handlers.keys()))


@app.list_tools()
async def list_tools() -> list[Tool]:
    # MCP lifecycle: client sends tools/list → this callback returns JSON schema definitions.
    return [h.get_tool_description() for h in _tool_handlers.values()]


@app.call_tool()
async def call_tool(
    name: str, arguments: Any
) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    # MCP lifecycle: client sends tools/call → validate → execute handler → return content.
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        try:
            arguments = dict(arguments)
        except Exception:
            raise RuntimeError("Tool arguments must be a dictionary")

    handler = _tool_handlers.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")

    try:
        logger.debug(f"Executing tool '{name}' with args: {arguments}")
        return await handler.run_tool(arguments)
    except asyncio.TimeoutError as exc:
        logger.error(f"Tool '{name}' timed out after {handler.__class__.__name__}: {exc}")
        return [
            TextContent(
                type="text",
                text=(
                    f"Tool execution timeout: The search operation took too long to complete. "
                    f"This may indicate a network issue or that the search service is slow. "
                    f"Please try again in a moment.\n\nDetails: {exc}"
                ),
            )
        ]
    except ValueError as exc:
        logger.warning(f"Tool validation error in '{name}': {exc}")
        return [
            TextContent(
                type="text",
                text=f"Input validation error: {exc}",
            )
        ]
    except Exception as exc:
        logger.exception(f"Unexpected error running tool '{name}': {exc}")
        return [
            TextContent(
                type="text",
                text=(
                    f"Error executing tool '{name}': {exc}\n\n"
                    f"This may be a temporary issue. Please try again.\n\n"
                    f"Technical details: {traceback.format_exc()}"
                ),
            )
        ]


# ──────────────────────────────────────────────────────────────────────────────
# Transport factories
# ──────────────────────────────────────────────────────────────────────────────

def create_sse_starlette_app(mcp_server: Server) -> Starlette:
    """
    SSE transport wiring with enhanced error handling:
      - SseServerTransport manages the SSE channel and a matching POST endpoint.
      - _SSEEndpoint.connect_sse() yields (read_stream, write_stream) for the session.
      - mcp_server.run() drives the full MCP request/response lifecycle over those streams.
      - Graceful error handling for connection failures and timeouts.
    """
    # sse_transport = SseServerTransport("/messages/")
    sse_transport = SseServerTransport("/messages")

    class _SSEEndpoint:
        async def __call__(self, scope, receive, send) -> None:
            logger.info("SSE client connected")
            try:
                async with sse_transport.connect_sse(scope, receive, send) as (read, write):
                    await mcp_server.run(
                        read, 
                        write,
                        mcp_server.create_initialization_options()
                    )
            except Exception as e:
                logger.error(f"SSE connection error: {e}", exc_info=True)

        # async def __call__(self, scope, receive, send) -> None:
        #     logger.debug("SSE client connected")
        #     async with sse_transport.connect_sse(scope, receive, send) as (read, write):
        #         await mcp_server.run(
        #             read,
        #             write,
        #             mcp_server.create_initialization_options(),
        #         )

    class _MessagesEndpoint:            
        async def __call__(self, scope, receive, send) -> None:
            logger.info("Message received from client")
            await sse_transport.handle_post_message(scope, receive, send)

            # await sse_transport.handle_post_message(scope, receive, send)

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/sse", endpoint=_SSEEndpoint()),
            Route("/sse/", endpoint=_SSEEndpoint()),
            Route("/messages", endpoint=_MessagesEndpoint(), methods=["POST", "OPTIONS"]),
            Route("/messages/", endpoint=_MessagesEndpoint(), methods=["POST", "OPTIONS"]),
        ],
    )
    starlette_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=86400,
    )
    return starlette_app


def create_streamable_http_app(mcp_server: Server) -> Starlette:
    """
    Streamable HTTP transport wiring with enhanced error handling:
      - StreamableHTTPSessionManager wraps the MCP server and handles session state.
      - json_response=False → responses are sent as chunked HTTP frames (streaming).
      - The lifespan context manager starts/stops the session manager cleanly.
      - Clients must echo back the `mcp-session-id` header on subsequent requests.
      - Request timeout protection and graceful error recovery.
    """
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,   # enables chunked/streaming response framing
        stateless=False,
    )

    class _MCPRoute:
        async def __call__(self, scope, receive, send) -> None:
            try:
                # Add per-request timeout (60 seconds)
                await asyncio.wait_for(
                    session_manager.handle_request(scope, receive, send),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Streamable HTTP request timeout")
                try:
                    await send({
                        "type": "http.response.start",
                        "status": 504,
                        "headers": [[b"content-type", b"application/json"]],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"error":"Request timeout"}',
                    })
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error handling streamable HTTP request: {e}", exc_info=True)
                try:
                    await send({
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [[b"content-type", b"application/json"]],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"error":"Internal Server Error"}',
                    })
                except Exception:
                    pass

    @contextlib.asynccontextmanager
    async def _lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            async with session_manager.run():
                yield
        except Exception as e:
            logger.error(f"Error in session manager lifespan: {e}", exc_info=True)
            raise

    starlette_app = Starlette(
        debug=False,
        routes=[Route("/mcp", endpoint=_MCPRoute())],
        lifespan=_lifespan,
    )
    starlette_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
        max_age=86400,
    )
    return starlette_app


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

async def run_server(mode: str, host: str, port: int) -> None:
    """
    Run the MCP server in the specified transport mode.
    
    Modes:
      - stdio: reads from stdin, writes to stdout (no network)
      - sse: Server-Sent Events over HTTP
      - streamable-http: Chunked HTTP streaming
      
    Includes error handling for network failures, timeouts, and graceful shutdown.
    """
    if mode == "stdio":
        logger.info("Running in STDIO mode")
        try:
            from mcp.server.stdio import stdio_server
            async with stdio_server() as (read_stream, write_stream):
                try:
                    await app.run(
                        read_stream,
                        write_stream,
                        app.create_initialization_options(),
                    )
                except BrokenPipeError:
                    logger.info("STDIO connection closed by client")
                except Exception as e:
                    logger.error(f"Error in STDIO mode: {e}", exc_info=True)
                    raise
        except ImportError as e:
            logger.error(f"Failed to import stdio_server: {e}")
            raise
        return

    if mode == "sse":
        logger.info("Running SSE mode at http://%s:%d/sse", host, port)
        try:
            starlette_app = create_sse_starlette_app(app)
            config = uvicorn.Config(
                starlette_app,
                host=host,
                port=port,
                log_level="info",
                access_log=True,
                timeout_keep_alive=65,
                timeout_notify=30,
            )
            server = uvicorn.Server(config)
            logger.info("SSE server initialized successfully")
            await server.serve()
        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(
                    f"Port {port} is already in use. Please specify a different port with --port"
                )
            else:
                logger.error(f"Network error in SSE mode: {e}")
            raise
        except Exception as e:
            logger.error(f"Error in SSE mode: {e}", exc_info=True)
            raise
        return

    if mode == "streamable-http":
        logger.info("Running Streamable HTTP mode at http://%s:%d/mcp", host, port)
        try:
            starlette_app = create_streamable_http_app(app)
            config = uvicorn.Config(
                starlette_app,
                host=host,
                port=port,
                log_level="info",
                access_log=True,
                timeout_keep_alive=65,
                timeout_notify=30,
            )
            server = uvicorn.Server(config)
            logger.info("Streamable HTTP server initialized successfully")
            await server.serve()
        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(
                    f"Port {port} is already in use. Please specify a different port with --port"
                )
            else:
                logger.error(f"Network error in Streamable HTTP mode: {e}")
            raise
        except Exception as e:
            logger.error(f"Error in Streamable HTTP mode: {e}", exc_info=True)
            raise
        return

    raise ValueError(f"Unsupported mode: {mode}. Choose from: stdio, sse, streamable-http")


async def main() -> None:
    """
    Entry point for the MCP server.
    
    Parses command-line arguments and starts the server in the specified mode.
    Handles initialization errors and provides helpful error messages.
    """
    parser = argparse.ArgumentParser(
        description="DuckDuckGo Browser MCP server — Real-time search via https://lite.duckduckgo.com/lite/",
        epilog=(
            "Examples:\n"
            "  python -m duckduckgo_browser --mode stdio\n"
            "  python -m duckduckgo_browser --mode sse --port 7070\n"
            "  python -m duckduckgo_browser --mode streamable-http --host 0.0.0.0 --port 8000"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    def _normalize_mode(raw_mode: str) -> str:
        return (raw_mode or "").strip().lower().replace("_", "-")
    
    env_mode = _normalize_mode(os.getenv("TRANSPORT_TYPE", "streamable-http"))
    env_host = os.getenv("APP_HOST", "0.0.0.0")
    env_port = os.getenv("APP_PORT")

    parser.add_argument(
        "--mode",
        choices=["stdio", "sse", "streamable-http"],
        default=env_mode,
        help="Transport mode (default: TRANSPORT_TYPE env or streamable-http)",
    )

    parser.add_argument(
        "--host",
        default=env_host,
        help="Bind host (default: APP_HOST env or 0.0.0.0)",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=env_port,
        help="Bind port (default: APP_PORT env)",
    )

    args = parser.parse_args()

    logger.info("="*80)
    logger.info("Starting DuckDuckGo Browser MCP Server")
    logger.info("="*80)
    logger.info("Python: %s", sys.version.split()[0])
    logger.info("Mode: %s", args.mode)
    if args.mode in ("sse", "streamable-http"):
        logger.info("Listening at http://%s:%d", args.host, args.port)
    logger.info("="*80)

    try:
        register_all_tools()
        logger.debug("Tools registered successfully")
        await run_server(args.mode, args.host, args.port)
    except KeyboardInterrupt:
        logger.info("Server stopped by user (Ctrl+C)")
    except ImportError as e:
        logger.error(f"Import error: {e}")
        logger.error("Make sure all dependencies are installed: pip install -e .")
        sys.exit(1)
    except OSError as e:
        if "Address already in use" in str(e):
            logger.error(f"Port {args.port} is already in use. Try: python -m duckduckgo_browser --port {args.port + 1}")
        else:
            logger.error(f"OS error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Server shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
