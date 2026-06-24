"""
Tool handler for `get_internet_result`.

Accepts a natural-language query, performs a real-time search on DuckDuckGo,
and returns a concise 3-4 line internet-based answer.
"""
from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote_plus
from urllib.parse import parse_qs, unquote, urlparse

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool

from ..services import get_search_engine
from .toolhandler import ToolHandler


class GetInternetResultToolHandler(ToolHandler):
    def __init__(self) -> None:
        super().__init__("get_internet_result")
        self._engine = get_search_engine()

    def get_tool_description(self) -> Tool:
        return Tool(
            name=self.name,
            description=(
                "Simple internet search via DuckDuckGo. "
                "Returns a concise 3-4 line answer with source links."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "input_value": {
                        "type": "string",
                        "description": (
                            "The search query or natural language question. "
                            "Examples: 'What is machine learning?', 'best travel destinations in Europe', "
                            "'how to invest in mutual funds'"
                        ),
                    }
                },
                "required": ["input_value"],
            },
        )

    async def run_tool(
        self, args: dict
    ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        self.validate_required_args(args, ["input_value"])

        query = str(args["input_value"]).strip()
        response = await self._engine.search(query)

        if not response.results:
            return [
                TextContent(
                    type="text",
                    text=(
                        "Answer: I could not find reliable results right now.\n"
                        "Source 1: N/A\n"
                        "Source 2: N/A"
                    ),
                )
            ]

        top = response.results[0]

        snippet_candidates = [
            (r.snippet or "").strip().replace("\n", " ")
            for r in response.results
            if (r.snippet or "").strip()
        ]

        s1 = snippet_candidates[0] if snippet_candidates else (top.title or "")
        s2 = snippet_candidates[1] if len(snippet_candidates) > 1 else ""
        if s2 and (
            "category" in s2.lower()
            or len(s2) < 40
            or s2.lower() == s1.lower()
        ):
            s2 = ""

        line1 = self._trim_sentence(s1, 260)
        answer = self._trim_sentence(line1, 320)

        src1 = self._canonical_url(top.url)
        src2 = src1
        for r in response.results[1:]:
            candidate = self._canonical_url(r.url)
            if candidate != src1:
                src2 = candidate
                break
        if src2 == src1:
            google_fallback = f"https://www.google.com/search?q={quote_plus(query)}"
            bing_fallback = f"https://www.bing.com/search?q={quote_plus(query)}"
            src2 = google_fallback if src1 != google_fallback else bing_fallback

        lines = [
            f"Answer: {answer}",
            f"Source 1: {src1}",
            f"Source 2: {src2}",
        ]

        return [TextContent(type="text", text="\n".join(lines))]

    def _trim_sentence(self, text: str, max_len: int) -> str:
        text = " ".join(text.split())
        if len(text) <= max_len:
            return text
        cutoff = text[: max_len - 3]
        if " " in cutoff:
            cutoff = cutoff.rsplit(" ", 1)[0]
        return cutoff.rstrip() + "..."

    def _merge_summary(self, first: str, second: str) -> str:
        """Create one clean answer line from two short snippets."""
        first = first.strip().rstrip(".")
        second = second.strip().rstrip(".")
        if not second:
            return first + "."
        if first.lower() == second.lower():
            return first + "."
        merged = f"{first}. {second}."
        return self._trim_sentence(merged, 320)

    def _canonical_url(self, url: str) -> str:
        """Extract real destination from DuckDuckGo redirect URLs when present."""
        try:
            if "duckduckgo.com/l/?" in url and "uddg=" in url:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                dest = qs.get("uddg", [""])[0]
                if dest:
                    return unquote(dest)
            if url.startswith("//"):
                return "https:" + url
            return url
        except Exception:
            return url
