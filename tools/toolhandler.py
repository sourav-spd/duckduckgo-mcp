from abc import ABC, abstractmethod
from collections.abc import Sequence

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool


class ToolHandler(ABC):
    """Base contract for every MCP tool registered in this server."""

    def __init__(self, tool_name: str) -> None:
        self.name = tool_name

    @abstractmethod
    def get_tool_description(self) -> Tool:
        raise NotImplementedError

    @abstractmethod
    async def run_tool(
        self, args: dict
    ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        raise NotImplementedError

    def validate_required_args(self, args: dict, required_fields: list[str]) -> None:
        missing = [f for f in required_fields if f not in args or args[f] in (None, "")]
        if missing:
            raise RuntimeError(f"Missing required argument(s): {', '.join(missing)}")
