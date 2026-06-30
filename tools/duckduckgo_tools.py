"""duckduckgo_tools.py - DuckDuckGo search tool handler, engine, and scraper."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool

from tools.toolhandler import ToolHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Web Scraper
# ---------------------------------------------------------------------------

class DuckDuckGoScraper:
    BASE_URLS = (
        "https://lite.duckduckgo.com/lite/",
        "https://html.duckduckgo.com/html/",
        "https://duckduckgo.com/html/",
    )
    INSTANT_ANSWER_URL = "https://api.duckduckgo.com/"

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        cache_ttl: int = 300,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.cache: dict[str, tuple[float, list[dict]]] = {}
        self.cache_ttl = cache_ttl
        self._default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        self._import_dependencies()

    def _import_dependencies(self) -> None:
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ImportError("requests library not found. Install with: pip install requests")
        try:
            from bs4 import BeautifulSoup
            self.BeautifulSoup = BeautifulSoup
        except ImportError:
            raise ImportError("beautifulsoup4 not found. Install with: pip install beautifulsoup4")
        try:
            import truststore
            truststore.inject_into_ssl()
            logger.info("Enabled OS trust store via truststore")
        except Exception:
            logger.debug("truststore not available; using default certifi bundle")

    def _get_from_cache(self, query: str) -> Optional[list[dict]]:
        if query not in self.cache:
            return None
        cached_time, results = self.cache[query]
        if time.time() - cached_time < self.cache_ttl:
            if results and all(not (r.get("snippet") or "").strip() for r in results):
                del self.cache[query]
                return None
            return results
        del self.cache[query]
        return None

    def _set_cache(self, query: str, results: list[dict]) -> None:
        self.cache[query] = (time.time(), results)

    def _parse_html_results(self, html_content: str) -> list[dict]:
        try:
            soup = self.BeautifulSoup(html_content, "html.parser")
        except Exception:
            return []

        results = []
        rows = soup.find_all("tr")
        i = 0
        while i < len(rows):
            try:
                link_tag = rows[i].find("a")
                if not link_tag or "href" not in link_tag.attrs:
                    i += 1
                    continue
                raw_title = html.unescape(link_tag.get_text(" ", strip=True))
                href = (link_tag.attrs.get("href") or "").strip()
                if not ("uddg=" in href or href.startswith("http") or href.startswith("//")):
                    i += 1
                    continue
                title = raw_title
                parts = raw_title.split(".", 1)
                if len(parts) == 2 and parts[0].strip().isdigit():
                    title = parts[1].strip()
                url = link_tag.attrs["href"].strip()
                if not title or not url:
                    i += 1
                    continue
                snippet = ""
                if i + 1 < len(rows):
                    next_text = html.unescape(rows[i + 1].get_text(" ", strip=True))
                    if next_text and not re.match(r"^\s*\d+\.\s+", next_text):
                        snippet = re.sub(r"\s+", " ", next_text).strip()
                        snippet = re.sub(r"\[[0-9]+\]", "", snippet)
                results.append({"title": title, "snippet": snippet, "url": url})
                i += 2
            except Exception:
                i += 1

        if not results:
            for a_tag in soup.select("a.result-link, a.result__a"):
                try:
                    url = (a_tag.get("href") or "").strip()
                    title = html.unescape(a_tag.get_text(strip=True))
                    if not title or not url:
                        continue
                    container = a_tag.find_parent("div", class_="result") or a_tag.parent
                    snippet = ""
                    if container:
                        snippet_tag = container.select_one("a.result-snippet, .result__snippet")
                        if snippet_tag:
                            snippet = html.unescape(snippet_tag.get_text(" ", strip=True))
                    results.append({"title": title, "snippet": snippet, "url": url})
                except Exception:
                    continue

        logger.info("Parsed %d results from DuckDuckGo", len(results))
        return results

    async def search_with_retry(self, query: str) -> list[dict]:
        cached = self._get_from_cache(query)
        if cached is not None:
            return cached
        for attempt in range(self.max_retries):
            try:
                results = await self._fetch_results(query)
                if results:
                    self._set_cache(query, results)
                    return results
            except asyncio.TimeoutError:
                logger.warning("Timeout attempt %d/%d for: %s", attempt + 1, self.max_retries, query)
            except Exception as e:
                logger.warning("Error attempt %d/%d: %s", attempt + 1, self.max_retries, e)
            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_backoff_base ** attempt)
        old_cache = self.cache.get(query)
        if old_cache:
            return old_cache[1]
        return []

    async def _fetch_results(self, query: str) -> list[dict]:
        params = {"q": query}
        ssl_error_seen = False
        for endpoint in self.BASE_URLS:
            try:
                response = await self._request(endpoint, params=params, verify_ssl=True)
                results = self._parse_html_results(response.text)
                if results:
                    return results
            except Exception as e:
                if "SSL" in type(e).__name__ or "SSL" in str(e):
                    ssl_error_seen = True
                logger.warning("Endpoint %s failed: %s", endpoint, e)
        if ssl_error_seen:
            try:
                response = await self._request(self.BASE_URLS[0], params=params, verify_ssl=False)
                parsed = self._parse_html_results(response.text)
                if parsed:
                    return parsed
            except Exception:
                pass
        try:
            api_results = await self._fetch_instant_answer(query, verify_ssl=not ssl_error_seen)
            if api_results:
                return api_results
        except Exception:
            pass
        return []

    async def _request(self, endpoint: str, params: dict, verify_ssl: bool):
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: self.requests.get(
                    endpoint,
                    params=params,
                    headers=self._default_headers,
                    timeout=self.timeout,
                    verify=verify_ssl,
                ),
            ),
            timeout=self.timeout + 1.0,
        )
        response.raise_for_status()
        return response

    async def _fetch_instant_answer(self, query: str, verify_ssl: bool) -> list[dict]:
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        response = await self._request(self.INSTANT_ANSWER_URL, params=params, verify_ssl=verify_ssl)
        data = response.json()
        results: list[dict] = []
        abstract = (data.get("AbstractText") or "").strip()
        abstract_url = (data.get("AbstractURL") or "").strip()
        heading = (data.get("Heading") or query).strip()
        if abstract:
            results.append({"title": heading, "snippet": abstract, "url": abstract_url or "https://duckduckgo.com/"})
        for topic in data.get("RelatedTopics") or []:
            text = (topic.get("Text") or "").strip() if isinstance(topic, dict) else ""
            url = (topic.get("FirstURL") or "").strip() if isinstance(topic, dict) else ""
            if text and url:
                results.append({"title": text.split(" - ")[0], "snippet": text, "url": url})
            if len(results) >= 2:
                break
        return results


_scraper_instance: Optional[DuckDuckGoScraper] = None


def get_scraper() -> DuckDuckGoScraper:
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = DuckDuckGoScraper()
    return _scraper_instance


# ---------------------------------------------------------------------------
# Search Engine
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str
    score: int


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult]
    dominant_category: str


def _detect_category(url: str, title: str, snippet: str) -> str:
    text = (url + " " + title + " " + snippet).lower()
    patterns = {
        "ai": r"\b(ai|artificial\s+intelligence|machine\s+learning|deep\s+learning|llm|gpt|neural|nlp)\b",
        "programming": r"\b(python|java|javascript|code|programming|github|stackoverflow|docker|api)\b",
        "cloud": r"\b(aws|cloud|kubernetes|terraform|azure|gcp|devops|container|infrastructure)\b",
        "finance": r"\b(stock|finance|invest|bitcoin|crypto|trading|market|forex|fund)\b",
        "health": r"\b(health|medical|doctor|hospital|disease|treatment|vaccine|medicine)\b",
        "science": r"\b(science|research|quantum|nasa|physics|chemistry|biology|experiment)\b",
        "education": r"\b(course|learn|university|education|school|academy|student|tutorial)\b",
        "travel": r"\b(travel|destination|hotel|flight|visa|tourism|trip|airport|vacation)\b",
        "food": r"\b(recipe|cook|food|cuisine|ingredient|restaurant|meal|diet|bake)\b",
        "sports": r"\b(sports|football|basketball|soccer|game|score|team|league|athlete)\b",
    }
    for category, pattern in patterns.items():
        if re.search(pattern, text):
            return category
    return "general"


class RealSearchEngine:
    TOP_N = 5

    def __init__(self) -> None:
        self._scraper: Optional[DuckDuckGoScraper] = None

    def _get_scraper(self) -> DuckDuckGoScraper:
        if self._scraper is None:
            self._scraper = get_scraper()
        return self._scraper

    async def search(self, query: str) -> SearchResponse:
        logger.info("Searching for: %s", query)
        try:
            web_results = await self._get_scraper().search_with_retry(query)
        except Exception as e:
            logger.error("Scraper error: %s", e)
            web_results = []

        if not web_results:
            return SearchResponse(query=query, results=[], dominant_category="general")

        results: list[SearchResult] = []
        dominant_category = "general"
        for i, r in enumerate(web_results[: self.TOP_N]):
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            url = r.get("url", "")
            if i == 0:
                dominant_category = _detect_category(url, title, snippet)
            results.append(SearchResult(title=title, snippet=snippet, url=url, score=len(web_results) - i))

        return SearchResponse(query=query, results=results, dominant_category=dominant_category)


_engine_instance: Optional[RealSearchEngine] = None


def get_search_engine() -> RealSearchEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RealSearchEngine()
    return _engine_instance


# ---------------------------------------------------------------------------
# Tool Handler
# ---------------------------------------------------------------------------

class GetInternetResultToolHandler(ToolHandler):
    def __init__(self) -> None:
        super().__init__("get_internet_result")
        self._engine = get_search_engine()

    def get_tool_description(self) -> Tool:
        return Tool(
            name=self.name,
            description="Simple internet search via DuckDuckGo. Returns a concise answer with source links.",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_value": {
                        "type": "string",
                        "description": "Search query",
                    }
                },
                "required": ["input_value"],
            },
        )

    async def run_tool(self, args: dict) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        self.validate_required_args(args, ["input_value"])
        query = str(args["input_value"]).strip()
        response = await self._engine.search(query)

        if not response.results:
            return [TextContent(type="text", text="Answer: I could not find reliable results right now.\nSource 1: N/A\nSource 2: N/A")]

        top = response.results[0]
        snippet_candidates = [
            (r.snippet or "").strip().replace("\n", " ")
            for r in response.results
            if (r.snippet or "").strip()
        ]
        s1 = snippet_candidates[0] if snippet_candidates else (top.title or "")
        s2 = snippet_candidates[1] if len(snippet_candidates) > 1 else ""
        if s2 and ("category" in s2.lower() or len(s2) < 40 or s2.lower() == s1.lower()):
            s2 = ""

        answer = self._trim_sentence(s1, 320)
        src1 = self._canonical_url(top.url)
        src2 = src1
        for r in response.results[1:]:
            candidate = self._canonical_url(r.url)
            if candidate != src1:
                src2 = candidate
                break
        if src2 == src1:
            src2 = f"https://www.google.com/search?q={quote_plus(query)}"

        return [TextContent(type="text", text=f"Answer: {answer}\nSource 1: {src1}\nSource 2: {src2}")]

    def _trim_sentence(self, text: str, max_len: int) -> str:
        text = " ".join(text.split())
        if len(text) <= max_len:
            return text
        cutoff = text[: max_len - 3]
        if " " in cutoff:
            cutoff = cutoff.rsplit(" ", 1)[0]
        return cutoff.rstrip() + "..."

    def _canonical_url(self, url: str) -> str:
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
