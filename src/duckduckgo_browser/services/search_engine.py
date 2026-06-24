"""
Real-time search engine using DuckDuckGo Lite endpoint.

Features:
  - Fetches live results from https://lite.duckduckgo.com/lite/
  - Automatic retry with exponential backoff on failures
  - Result caching for resilience
  - Graceful fallback to cached data on network errors
  - Category detection based on URL/title patterns
  - Curated website suggestions by category
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Category → curated website suggestions (shown in output)
_CATEGORY_SITES: dict[str, list[dict]] = {
    "ai": [
        {"name": "Hugging Face", "url": "https://huggingface.co", "reason": "Leading hub for open-source AI models and datasets"},
        {"name": "Papers With Code", "url": "https://paperswithcode.com", "reason": "Research papers paired with reproducible code"},
        {"name": "Towards Data Science", "url": "https://towardsdatascience.com", "reason": "Practical AI/ML tutorials and articles"},
    ],
    "programming": [
        {"name": "Stack Overflow", "url": "https://stackoverflow.com", "reason": "Largest Q&A community for developers"},
        {"name": "GitHub", "url": "https://github.com", "reason": "Source code hosting and open-source collaboration"},
        {"name": "MDN Web Docs", "url": "https://developer.mozilla.org", "reason": "Authoritative web technology reference"},
    ],
    "cloud": [
        {"name": "AWS Documentation", "url": "https://docs.aws.amazon.com", "reason": "Official AWS service documentation"},
        {"name": "Cloud Native Computing Foundation", "url": "https://www.cncf.io", "reason": "Kubernetes, Prometheus, and cloud-native projects"},
        {"name": "HashiCorp Learn", "url": "https://developer.hashicorp.com", "reason": "Terraform, Vault, and Consul tutorials"},
    ],
    "finance": [
        {"name": "Investopedia", "url": "https://www.investopedia.com", "reason": "Comprehensive financial education and definitions"},
        {"name": "Bloomberg Markets", "url": "https://www.bloomberg.com/markets", "reason": "Real-time market data and financial news"},
        {"name": "Morningstar", "url": "https://www.morningstar.com", "reason": "Independent investment research and fund ratings"},
    ],
    "health": [
        {"name": "Mayo Clinic", "url": "https://www.mayoclinic.org", "reason": "Trusted medical information from a leading hospital"},
        {"name": "WebMD", "url": "https://www.webmd.com", "reason": "Symptom checker and health condition guides"},
        {"name": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov", "reason": "Peer-reviewed biomedical research database"},
    ],
    "science": [
        {"name": "Nature", "url": "https://www.nature.com", "reason": "Premier peer-reviewed scientific journal"},
        {"name": "NASA", "url": "https://www.nasa.gov", "reason": "Space exploration and Earth science data"},
        {"name": "ScienceDirect", "url": "https://www.sciencedirect.com", "reason": "Elsevier's database of scientific articles"},
    ],
    "education": [
        {"name": "Coursera", "url": "https://www.coursera.org", "reason": "University-backed online courses and degrees"},
        {"name": "edX", "url": "https://www.edx.org", "reason": "MIT and Harvard-founded MOOC platform"},
        {"name": "Khan Academy", "url": "https://www.khanacademy.org", "reason": "Free K-12 and college-level education"},
    ],
    "travel": [
        {"name": "Lonely Planet", "url": "https://www.lonelyplanet.com", "reason": "Comprehensive destination travel guides"},
        {"name": "TripAdvisor", "url": "https://www.tripadvisor.com", "reason": "Hotel, restaurant, and attraction reviews"},
        {"name": "Skyscanner", "url": "https://www.skyscanner.com", "reason": "Flight and hotel price comparison"},
    ],
    "food": [
        {"name": "Allrecipes", "url": "https://www.allrecipes.com", "reason": "Millions of community-rated recipes"},
        {"name": "Food Network", "url": "https://www.foodnetwork.com", "reason": "Professional chef recipes and cooking shows"},
        {"name": "Serious Eats", "url": "https://www.seriouseats.com", "reason": "Science-backed cooking techniques and recipes"},
    ],
    "sports": [
        {"name": "ESPN", "url": "https://www.espn.com", "reason": "Live scores, news, and analysis across all sports"},
        {"name": "BBC Sport", "url": "https://www.bbc.com/sport", "reason": "Global sports coverage from the BBC"},
        {"name": "The Athletic", "url": "https://theathletic.com", "reason": "In-depth sports journalism and analysis"},
    ],
    "general": [
        {"name": "Wikipedia", "url": "https://en.wikipedia.org", "reason": "Free encyclopaedia covering virtually every topic"},
        {"name": "Reddit", "url": "https://www.reddit.com", "reason": "Community discussions and crowd-sourced answers"},
        {"name": "Quora", "url": "https://www.quora.com", "reason": "Expert answers to questions on any subject"},
    ],
}
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
    suggested_sites: list[dict]
    dominant_category: str


def _detect_category(url: str, title: str, snippet: str) -> str:
    """
    Heuristically detect content category from URL, title, and snippet.
    
    Returns one of: ai, programming, cloud, finance, health, science, 
                    education, travel, food, sports, general
    """
    text = (url + " " + title + " " + snippet).lower()
    
    patterns = {
        "ai": r"\b(ai|artificial\s+intelligence|machine\s+learning|deep\s+learning|llm|gpt|neural|nlp|mcp|model\s+context|anthropic)\b",
        "programming": r"\b(python|java|javascript|code|programming|github|stackoverflow|docker|api|framework|library|developer)\b",
        "cloud": r"\b(aws|cloud|kubernetes|terraform|azure|gcp|devops|container|infrastructure|deploy|server)\b",
        "finance": r"\b(stock|finance|invest|bitcoin|crypto|trading|market|forex|fund|portfolio|bank)\b",
        "health": r"\b(health|medical|doctor|hospital|disease|treatment|vaccine|medicine|clinic|wellness)\b",
        "science": r"\b(science|research|quantum|nasa|physics|chemistry|biology|experiment|paper|journal)\b",
        "education": r"\b(course|learn|university|education|school|academy|student|mooc|tutorial|training)\b",
        "travel": r"\b(travel|destination|hotel|flight|visa|tourism|trip|airport|vacation|guide)\b",
        "food": r"\b(recipe|cook|food|cuisine|ingredient|restaurant|meal|diet|bake|kitchen)\b",
        "sports": r"\b(sports|football|basketball|soccer|game|score|team|league|athlete|tournament)\b",
    }
    
    for category, pattern in patterns.items():
        if re.search(pattern, text):
            return category
    
    return "general"


class RealSearchEngine:
    """
    Real-time search engine using DuckDuckGo Lite endpoint.
    
    Performs actual web searches with:
      - Automatic retry with exponential backoff
      - Result caching for resilience
      - Category detection from results
      - Curated website suggestions
    """

    TOP_N = 5  # max results returned

    def __init__(self) -> None:
        """Initialize the search engine."""
        self._scraper: Optional[object] = None

    def _get_scraper(self) -> object:
        """Lazy-load the web scraper to avoid import errors if not installed."""
        if self._scraper is None:
            try:
                from .web_scraper import get_scraper
                self._scraper = get_scraper()
            except ImportError as e:
                logger.error(f"Failed to import web scraper: {e}")
                raise
        return self._scraper

    async def search(self, query: str) -> SearchResponse:
        """
        Perform a real-time search via DuckDuckGo Lite.
        
        Args:
            query: Search query string
            
        Returns:
            SearchResponse with results and suggestions
        """
        logger.info(f"Searching for: {query}")
        
        try:
            scraper = self._get_scraper()
            web_results = await scraper.search_with_retry(query)
        except Exception as e:
            logger.error(f"Scraper error: {e}")
            web_results = []

        if not web_results:
            logger.warning(f"No results found for: {query}")
            return SearchResponse(
                query=query,
                results=[],
                suggested_sites=_CATEGORY_SITES.get("general", []),
                dominant_category="general",
            )

        # Convert web results to SearchResult objects
        results: list[SearchResult] = []
        dominant_category = "general"
        
        for i, web_result in enumerate(web_results[: self.TOP_N]):
            try:
                title = web_result.get("title", "")
                snippet = web_result.get("snippet", "")
                url = web_result.get("url", "")
                
                # Detect category from first result
                if i == 0:
                    dominant_category = _detect_category(url, title, snippet)
                
                results.append(SearchResult(
                    title=title,
                    snippet=snippet,
                    url=url,
                    score=len(web_results) - i,  # Higher score for earlier results
                ))
            except Exception as e:
                logger.debug(f"Error processing result {i}: {e}")
                continue

        suggested_sites = _CATEGORY_SITES.get(dominant_category, _CATEGORY_SITES["general"])

        return SearchResponse(
            query=query,
            results=results,
            suggested_sites=suggested_sites,
            dominant_category=dominant_category,
        )


# Singleton instance
_engine_instance: Optional[RealSearchEngine] = None


def get_search_engine() -> RealSearchEngine:
    """Get or create the search engine singleton."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RealSearchEngine()
    return _engine_instance


# Backwards compatibility alias for old code
DeterministicSearchEngine = RealSearchEngine
