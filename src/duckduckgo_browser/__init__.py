import asyncio
from .server import main as async_main


def main() -> None:
    """Synchronous package entry point for the CLI script."""
    asyncio.run(async_main())


__all__ = ["main", "async_main"]
