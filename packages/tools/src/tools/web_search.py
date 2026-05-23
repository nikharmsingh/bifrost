import httpx


async def web_search(query: str, num_results: int = 5) -> list[dict]:
    """Placeholder — swap in your preferred search API (Brave, Tavily, SerpAPI)."""
    raise NotImplementedError(
        "Configure a search provider. "
        "Recommended: set TAVILY_API_KEY and use the tavily-python client."
    )
