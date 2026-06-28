from duckduckgo_browser.services.search_engine import (
    DeterministicSearchEngine,  # Backwards compatibility
    RealSearchEngine,
    SearchResponse,
    SearchResult,
    get_search_engine,
)

__all__ = [
    "DeterministicSearchEngine",
    "RealSearchEngine",
    "SearchResponse",
    "SearchResult",
    "get_search_engine",
]
