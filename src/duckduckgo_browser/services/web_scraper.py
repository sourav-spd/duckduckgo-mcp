"""
Real-time web scraper for DuckDuckGo Lite endpoint.

Features:
  - Fetches actual search results from https://lite.duckduckgo.com/lite/
  - Retry logic with exponential backoff on failures
  - Configurable timeouts
  - Exception handling for network errors
  - HTML parsing using BeautifulSoup
  - Result caching to handle temporary network issues
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)


class DuckDuckGoScraper:
    """Robust DuckDuckGo Lite web scraper with retry logic and caching."""

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
        """
        Initialize the scraper.
        
        Args:
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            retry_backoff_base: Base for exponential backoff (seconds)
            cache_ttl: Cache time-to-live in seconds
        """
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
        """Dynamically import requests and BeautifulSoup."""
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ImportError(
                "requests library not found. Install with: pip install requests"
            )
        
        try:
            from bs4 import BeautifulSoup
            self.BeautifulSoup = BeautifulSoup
        except ImportError:
            raise ImportError(
                "beautifulsoup4 library not found. Install with: pip install beautifulsoup4"
            )

        # Optional: use OS trust store (Windows/macOS/Linux) without adding custom cert files.
        try:
            import truststore

            truststore.inject_into_ssl()
            logger.info("Enabled OS trust store via truststore")
        except Exception:
            logger.debug("truststore not available; using default certifi bundle")

    def _get_from_cache(self, query: str) -> Optional[list[dict]]:
        """Retrieve cached results if still valid."""
        if query not in self.cache:
            return None
        
        cached_time, results = self.cache[query]
        if time.time() - cached_time < self.cache_ttl:
            # Refresh old cache entries that were parsed without snippets.
            if results and all(not (r.get("snippet") or "").strip() for r in results):
                del self.cache[query]
                return None
            logger.debug(f"Cache hit for query: {query}")
            return results
        
        # Expired, remove from cache
        del self.cache[query]
        return None

    def _set_cache(self, query: str, results: list[dict]) -> None:
        """Store results in cache."""
        self.cache[query] = (time.time(), results)
        logger.debug(f"Cached results for query: {query}")

    def _parse_html_results(self, html_content: str) -> list[dict]:
        """
        Parse DuckDuckGo Lite HTML response.
        
        DuckDuckGo Lite returns results in a simple table format:
          <tr class="result">
            <td>
              <a href="...">Title</a>
              <span class="result-snippet">Snippet</span>
            </td>
          </tr>
        """
        try:
            soup = self.BeautifulSoup(html_content, "html.parser")
        except Exception as e:
            logger.error(f"Failed to parse HTML: {e}")
            return []

        results = []

        # DuckDuckGo Lite often renders results as alternating table rows:
        # one row for title/link, next row for snippet text.
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
                is_result_link = "uddg=" in href or href.startswith("http") or href.startswith("//")
                if not is_result_link:
                    i += 1
                    continue
                # Remove ranking prefix like "1. " or "10. "
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
                continue
            except Exception as e:
                logger.debug(f"Error parsing lite row pair: {e}")
                i += 1
                continue
        
        # Backward-compatible parser for alternate structures.
        for tr in soup.find_all("tr", class_="result"):
            try:
                # Extract link
                link_tag = tr.find("a")
                if not link_tag or "href" not in link_tag.attrs:
                    continue
                
                url = link_tag.attrs["href"]
                title = link_tag.get_text(strip=True)
                
                # Extract snippet
                snippet_tag = tr.find("span", class_="result-snippet")
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                
                # Decode HTML entities
                title = html.unescape(title)
                snippet = html.unescape(snippet)
                
                if title and url:
                    results.append({
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                    })
                    logger.debug(f"Parsed result: {title[:50]}... from {url}")
                    
            except Exception as e:
                logger.debug(f"Error parsing individual result: {e}")
                continue

        # DuckDuckGo HTML endpoint fallback structure
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
                except Exception as e:
                    logger.debug(f"Error parsing HTML endpoint result: {e}")
                    continue

        logger.info(f"Parsed {len(results)} results from DuckDuckGo Lite")
        return results

    async def search_with_retry(self, query: str) -> list[dict]:
        """
        Search DuckDuckGo Lite with retry logic.
        
        Args:
            query: Search query string
            
        Returns:
            List of result dicts with keys: title, snippet, url, source
        """
        # Check cache first
        cached = self._get_from_cache(query)
        if cached is not None:
            return cached

        logger.info(f"Searching DuckDuckGo Lite for: {query}")

        for attempt in range(self.max_retries):
            try:
                results = await self._fetch_results(query)
                if results:
                    self._set_cache(query, results)
                    return results
                    
            except asyncio.TimeoutError:
                logger.warning(
                    f"Timeout on attempt {attempt + 1}/{self.max_retries} for query: {query}"
                )
            except self.requests.RequestException as e:
                logger.warning(
                    f"Request failed on attempt {attempt + 1}/{self.max_retries}: {e}"
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error on attempt {attempt + 1}/{self.max_retries}: {e}"
                )

            # Exponential backoff before retry
            if attempt < self.max_retries - 1:
                backoff_time = self.retry_backoff_base ** attempt
                logger.debug(f"Retrying in {backoff_time:.1f}s...")
                await asyncio.sleep(backoff_time)

        logger.error(f"All {self.max_retries} attempts failed for query: {query}")
        # Return cached results even if expired, or empty list
        old_cache = self.cache.get(query)
        if old_cache:
            logger.info(f"Returning stale cache for: {query}")
            return old_cache[1]
        return []

    async def _fetch_results(self, query: str) -> list[dict]:
        """
        Fetch and parse results from DuckDuckGo Lite (single attempt without retry).
        
        Must be called with timeout wrapper in outer context.
        """
        params = {"q": query}
        ssl_error_seen = False

        for endpoint in self.BASE_URLS:
            try:
                response = await self._request(endpoint, params=params, verify_ssl=True)
                results = self._parse_html_results(response.text)
                if results:
                    return results
            except self.requests.exceptions.SSLError as e:
                ssl_error_seen = True
                logger.warning(f"SSL verification failed for {endpoint}: {e}")
            except asyncio.TimeoutError as e:
                logger.warning(f"Async timeout for {endpoint}: {e}")
            except self.requests.Timeout as e:
                logger.warning(f"Request timeout for {endpoint}: {e}")
            except self.requests.ConnectionError as e:
                logger.warning(f"Connection error for {endpoint}: {e}")
            except self.requests.HTTPError as e:
                logger.warning(f"HTTP error for {endpoint}: {e}")

        # Last resort for environments with custom/intercepting cert chains.
        if ssl_error_seen:
            try:
                logger.warning("Retrying with SSL verification disabled as last resort")
                response = await self._request(self.BASE_URLS[0], params=params, verify_ssl=False)
                parsed = self._parse_html_results(response.text)
                if parsed:
                    return parsed
            except Exception as e:
                logger.warning(f"Last-resort SSL-disabled attempt failed: {e}")

        # Fallback: DuckDuckGo Instant Answer API (still DuckDuckGo domain).
        try:
            api_results = await self._fetch_instant_answer(query, verify_ssl=not ssl_error_seen)
            if api_results:
                logger.info("Using DuckDuckGo Instant Answer fallback")
                return api_results
        except Exception as e:
            logger.warning(f"Instant Answer fallback failed: {e}")

        return []

    async def _request(self, endpoint: str, params: dict, verify_ssl: bool):
        """Execute a single HTTP request in a thread to keep async loop responsive."""
        if not verify_ssl:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

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
        logger.debug(f"Got response {response.status_code} from {endpoint}")
        return response

    async def _fetch_instant_answer(self, query: str, verify_ssl: bool) -> list[dict]:
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        response = await self._request(self.INSTANT_ANSWER_URL, params=params, verify_ssl=verify_ssl)
        data = response.json()

        results: list[dict] = []
        abstract = (data.get("AbstractText") or "").strip()
        abstract_url = (data.get("AbstractURL") or "").strip()
        heading = (data.get("Heading") or query).strip()
        if abstract:
            results.append(
                {
                    "title": heading,
                    "snippet": abstract,
                    "url": abstract_url or "https://duckduckgo.com/",
                }
            )

        for topic in data.get("RelatedTopics") or []:
            text = (topic.get("Text") or "").strip() if isinstance(topic, dict) else ""
            url = (topic.get("FirstURL") or "").strip() if isinstance(topic, dict) else ""
            if text and url:
                results.append({"title": text.split(" - ")[0], "snippet": text, "url": url})
            if len(results) >= 2:
                break

        return results


# Singleton instance
_scraper_instance: Optional[DuckDuckGoScraper] = None


def get_scraper() -> DuckDuckGoScraper:
    """Get or create the scraper singleton."""
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = DuckDuckGoScraper()
    return _scraper_instance


async def scrape_search_results(query: str) -> list[dict]:
    """
    Convenience function to search DuckDuckGo Lite.
    
    Args:
        query: Search query
        
    Returns:
        List of result dicts with title, snippet, url
    """
    scraper = get_scraper()
    return await scraper.search_with_retry(query)
