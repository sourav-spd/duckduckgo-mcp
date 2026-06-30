"""duckduckgo_server.py - DuckDuckGo MCP Server with stdio, SSE and Streamable HTTP."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import traceback

# Ensure the tools/ directory is always importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections.abc import AsyncIterator, Sequence
from typing import Any, Dict

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from tools.duckduckgo_tools import GetInternetResultToolHandler
from tools.toolhandler import ToolHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("duckduckgo-mcp")

app = Server("duckduckgo-mcp")

_tool_handlers: Dict[str, ToolHandler] = {}


def add_tool_handler(handler: ToolHandler) -> None:
    _tool_handlers[handler.name] = handler
    logger.info("Registered tool: %s", handler.name)


def get_tool_handler(name: str) -> ToolHandler | None:
    return _tool_handlers.get(name)


def register_all_tools() -> None:
    add_tool_handler(GetInternetResultToolHandler())
    logger.info("Total tools registered: %d", len(_tool_handlers))


@app.list_tools()
async def list_tools() -> list[Tool]:
    tools = [h.get_tool_description() for h in _tool_handlers.values()]
    logger.info("list_tools -> %d tool(s) returned", len(tools))
    return tools


@app.call_tool()
async def call_tool(
    name: str, arguments: Any
) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        try:
            arguments = dict(arguments)
        except Exception:
            logger.error("arguments is not dict-like: %s", type(arguments))
            raise RuntimeError("Tool arguments must be a dictionary")

    handler = get_tool_handler(name)
    if not handler:
        logger.error("Unknown tool requested: %s", name)
        raise ValueError(f"Unknown tool: '{name}'")

    logger.info("Executing tool '%s' with keys: %s", name, list(arguments.keys()))
    try:
        result = await handler.run_tool(arguments)
        logger.info("Tool '%s' completed successfully", name)
        return result
    except Exception as exc:
        logger.exception("Tool '%s' raised an exception: %s", name, exc)
        return [
            TextContent(
                type="text",
                text=f"Error executing tool '{name}': {exc}\n\n{traceback.format_exc()}",
            )
        ]


def create_sse_starlette_app(mcp_server: Server) -> Starlette:
    sse_transport = SseServerTransport("/messages/")

    class _SSEEndpoint:
        async def __call__(self, scope, receive, send) -> None:
            async with sse_transport.connect_sse(scope, receive, send) as (
                read_stream,
                write_stream,
            ):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )

    async def _health_endpoint(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _root_endpoint(_request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": "sse", "server": "duckduckgo-mcp", "tools": len(_tool_handlers)})

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/", endpoint=_root_endpoint),
            Route("/health", endpoint=_health_endpoint),
            Route("/healthz", endpoint=_health_endpoint),
            Route("/sse", endpoint=_SSEEndpoint()),
            Mount("/messages/", app=sse_transport.handle_post_message),
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
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    class _StreamableHTTPRoute:
        async def __call__(self, scope, receive, send) -> None:
            await session_manager.handle_request(scope, receive, send)

    async def _health_endpoint(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _root_endpoint(_request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": "streamable-http", "server": "duckduckgo-mcp", "tools": len(_tool_handlers)})

    @contextlib.asynccontextmanager
    async def _lifespan(_starlette_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/", endpoint=_root_endpoint),
            Route("/health", endpoint=_health_endpoint),
            Route("/healthz", endpoint=_health_endpoint),
            Route("/mcp", endpoint=_StreamableHTTPRoute()),
        ],
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


async def run_server(mode: str, host: str = "0.0.0.0", port: int = 8000) -> None:
    if mode == "stdio":
        logger.info("Starting in STDIO mode")
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )

    elif mode == "sse":
        logger.info("Starting in SSE mode on http://%s:%d", host, port)
        starlette_app = create_sse_starlette_app(app)
        config = uvicorn.Config(app=starlette_app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    elif mode == "streamable-http":
        logger.info("Starting in STREAMABLE-HTTP mode on http://%s:%d", host, port)
        starlette_app = create_streamable_http_app(app)
        config = uvicorn.Config(app=starlette_app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    else:
        raise ValueError(f"Unknown server mode: '{mode}'")


async def main() -> None:
    def _normalize_mode(raw_mode: str) -> str:
        return (raw_mode or "").strip().lower().replace("_", "-")

    env_transport_mode = _normalize_mode(os.getenv("TRANSPORT_TYPE", ""))
    if env_transport_mode not in {"stdio", "sse", "streamable-http"}:
        env_transport_mode = ""

    default_mode = env_transport_mode or ("streamable-http" if os.getenv("APP_PORT") else "stdio")

    default_host = os.getenv("APP_HOST", "0.0.0.0")
    try:
        default_port = int(os.getenv("APP_PORT", os.getenv("PORT", "8000")))
    except ValueError:
        default_port = 8000

    parser = argparse.ArgumentParser(
        prog="duckduckgo-mcp",
        description="DuckDuckGo MCP Server",
    )
    parser.add_argument(
        "--mode",
        choices=["stdio", "sse", "streamable-http", "streamable_http"],
        default=default_mode,
        help="Transport mode",
    )
    parser.add_argument("--host", default=default_host, help="Bind host for HTTP modes")
    parser.add_argument("--port", type=int, default=default_port, help="Bind port for HTTP modes")
    args = parser.parse_args()

    logger.info("DuckDuckGo MCP Server starting up")
    logger.info("Python %s", sys.version)

    register_all_tools()
    logger.info("Tools available: %s", list(_tool_handlers.keys()))

    mode = "streamable-http" if args.mode == "streamable_http" else args.mode
    await run_server(mode=mode, host=args.host, port=args.port)


def cli_main() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
