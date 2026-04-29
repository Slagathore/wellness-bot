"""
Web Search Utilities for Wellness Bot

Mission: Enhance bot responses with real-time factual information when users ask questions
that require current data (sports scores, current events, factual queries).

Goals:
- Provide accurate, up-to-date information when wellness conversations require it
- Use LLM to intelligently detect when search is needed (not just keyword matching)
- Integrate search results seamlessly into conversational responses
- Maintain async/non-blocking operation to prevent message delays

Implementation:
- DuckDuckGo search API for privacy-friendly web search
- Async operations to prevent blocking user messages
- LLM-powered decision making for when to search
- Timeout protection (3 seconds max)

Dependencies: duckduckgo-search, ollama, asyncio
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from duckduckgo_search import DDGS  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - optional dependency
    DDGS = None
from ollama import chat

from app.config import settings
from app.utils.time_utils import operator_now

logger = logging.getLogger(__name__)


class WebSearchRateLimitError(RuntimeError):
    """Raised when the search provider responds with a rate limit error."""


_LAST_SEARCH_AT: float = 0.0
_RESULT_CACHE: Dict[str, Tuple[float, Optional[str]]] = {}
_CACHE_MAX_ENTRIES = 128


def _prune_cache(now: float, ttl: float) -> None:
    expired = [key for key, (ts, _) in _RESULT_CACHE.items() if now - ts > ttl]
    for key in expired:
        _RESULT_CACHE.pop(key, None)
    while len(_RESULT_CACHE) > _CACHE_MAX_ENTRIES and _RESULT_CACHE:
        _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))


def search_web(
    query: str, max_results: int = 3, timeout: int = 3
) -> List[Dict[str, str]] | None:
    """
    Synchronous web search using DuckDuckGo.

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default 3)
        timeout: Maximum time to wait for results in seconds (default 3)

    Returns:
        List of dicts with 'title', 'href', 'body' keys
        Returns None when rate limited, empty list on other errors

    Example:
        results = search_web("Dallas Cowboys score today")
        # [{'title': '...', 'href': '...', 'body': '...'}, ...]
    """
    if DDGS is None:
        logger.warning("DuckDuckGo search not available - install duckduckgo-search")
        return []
    try:
        # Small delay to avoid rate limiting
        time.sleep(0.5)

        # Use context manager for proper cleanup
        with DDGS() as ddgs:
            results = []
            # text() returns generator, convert to list with limit
            for result in ddgs.text(query, max_results=max_results):
                results.append(
                    {
                        "title": result.get("title", ""),
                        "href": result.get("href", ""),
                        "body": result.get("body", ""),
                    }
                )
                if len(results) >= max_results:
                    break
            return results
    except Exception as e:
        error_msg = str(e)
        if "Ratelimit" in error_msg or "202" in error_msg:
            logger.warning(
                "DuckDuckGo rate limit hit - search too frequent. Wait a few seconds."
            )
            raise WebSearchRateLimitError(error_msg) from e
        logger.error(f"Web search error: {e}")
        return []


async def search_web_async(
    query: str, max_results: int = 3, timeout: int = 3
) -> List[Dict[str, str]] | None:
    """
    Async wrapper for web search with timeout protection.

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default 3)
        timeout: Maximum time to wait in seconds (default 3)

    Returns:
        List of search results, None when rate limited, or empty list on timeout/error

    Example:
        results = await search_web_async("current weather NYC")
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(search_web, query, max_results), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"Web search timed out after {timeout}s for query: {query}")
        return []
    except WebSearchRateLimitError:
        raise
    except Exception as e:
        logger.error(f"Async web search error: {e}")
        return []


def format_search_results_for_prompt(results: List[Dict[str, str]] | None) -> str:
    """Format compact search results for LLM grounding rather than direct user display."""
    if not results:
        return ""
    lines = ["Live web results:"]
    for result in results[:3]:
        title = (result.get("title") or "").strip()
        snippet = (result.get("body") or "").strip()
        href = (result.get("href") or "").strip()
        if title:
            lines.append(f"- {title}")
        if snippet:
            lines.append(f"  {snippet[:220]}")
        if href:
            lines.append(f"  Source: {href}")
    return "\n".join(lines).strip()


async def search_context_async(
    query: str,
    *,
    max_results: int = 3,
    timeout: int = 2,
) -> str:
    """Run a bounded live search and return compact prompt-ready context."""
    results = await search_web_async(query, max_results=max_results, timeout=timeout)
    return format_search_results_for_prompt(results)


def search_context(
    query: str,
    *,
    max_results: int = 3,
    timeout: int = 2,
) -> str:
    """Sync wrapper for prompt-ready live search context."""
    results = search_web(query, max_results=max_results, timeout=timeout)
    return format_search_results_for_prompt(results)


def detect_search_need_with_llm(
    message: str, model: str | None = None
) -> Dict[str, Any]:
    """
    Use LLM to intelligently detect if a message needs web search.

    This is MUCH better than keyword matching because it understands context:
    - "How are the Cowboys doing?" → YES (current sports score)
    - "I'm doing well, how are you?" → NO (casual conversation)
    - "What's the weather like?" → YES (current weather)
    - "I feel like the weather is affecting my mood" → NO (wellness conversation)

    Args:
        message: User's message text
        model: Optional model to use for detection (defaults to settings().search_decision_model)

    Returns:
        {
            'needs_search': bool,
            'query': str or None (suggested search query),
            'reason': str (explanation)
        }

    Example:
        result = detect_search_need_with_llm("How are the Cowboys doing today?")
        # {'needs_search': True, 'query': 'Dallas Cowboys game today score', 'reason': '...'}
    """
    try:
        cfg = settings()
        chosen_model = model or getattr(cfg, "search_decision_model", None)
        if not chosen_model:
            raise RuntimeError("No search decision model configured")

        # Get current date dynamically
        now = operator_now()
        current_date = now.strftime("%B %d, %Y")  # e.g., "October 10, 2025"
        current_year = now.year
        current_month = now.strftime("%B %Y")  # e.g., "October 2025"

        prompt = f"""Analyze this message to determine if it needs real-time web search for accurate information.

**IMPORTANT: Current date is {current_date}. The current year is {current_year}.**

User message: "{message}"

SEARCH NEEDED for:
- Sports scores/results/standings ("Cowboys doing?", "who won", "game score", "Lakers record")
- Current events/news ("latest news", "what happened", "breaking story")
- Weather ("temperature", "forecast", "weather today")
- Factual lookups ("stock price", "current time in", "exchange rate")
- Recent happenings ("what's going on with", "any updates on")
- Entertainment releases ("movies coming out", "new games", "what's playing", "upcoming albums")
- Recommendations/lists ("best restaurants in", "top 10", "some of the best", "good places to")
- Food/dining ("restaurants near", "where to eat", "menu at", "reservations")
- Current availability ("is X open", "showtimes for", "in stock at")

NO SEARCH for:
- Casual greetings ("how are you", "what's up", "hey")
- Personal wellness talk ("I'm feeling", "struggling with", "help me cope")
- Roleplay/fantasy/sexual content (anything NSFW or fictional)
- Questions about past conversations ("what did we talk about", "earlier you said")
- Hypothetical/philosophical ("what if", "do you think")
- Requests for personal help ("can you help", "I need advice")

If it's clearly asking about REAL-WORLD current information (sports, news, weather, facts, entertainment, dining, recommendations), say YES.
If it's conversation, wellness, roleplay, or personal topics, say NO.
When in doubt, respond NO (needs_search: false).

CRITICAL: When creating search queries, use ONLY the current year {current_year} and/or current month {current_month}:
- Sports: "Dallas Cowboys latest game today {current_year}" (NOT 2024!)
- News: "latest news {current_month}"
- Entertainment: "movies releasing {current_month}"
- Always use {current_year}, never hardcode years

Examples using CORRECT current date:
- "How are Cowboys doing?" → query: "Dallas Cowboys latest game score {current_year}"
- "Movies coming out?" → query: "movies releasing now {current_month}"
- "Weather?" → query: "weather today current"

Respond in JSON format:
{{
    "needs_search": true/false,
    "query": "search query using {current_year} and {current_month}" or null,
    "reason": "brief explanation"
}}"""

        response = chat(
            model=chosen_model,
            messages=[{"role": "user", "content": prompt}],
            options={
                "temperature": 0.1,
                "request_timeout": 8,
            },  # Low temperature for consistent decisions
        )

        import json

        result = json.loads(response["message"]["content"])

        logger.info(
            f"[Search Decision] Message: '{message[:50]}...' → {result['needs_search']}"
        )
        return result

    except Exception as e:
        logger.warning(f"LLM search detection error: {e}")
        # Fallback to keyword-based detection
        return should_search_web(message)


def should_search_web(message: str) -> Dict[str, Any]:
    """
    Keyword-based fallback for detecting search need.
    Used when LLM detection fails.

    Args:
        message: User's message text

    Returns:
        Same format as detect_search_need_with_llm()
    """
    message_lower = message.lower()

    sports_triggers = [
        "cowboys",
        "game score",
        "final score",
        "match",
        "team playing",
        "who won",
        "who lost",
        "winning the game",
        "losing the game",
        "sports",
        "playoff",
    ]
    sports_cues = ["score", "playing", "won", "lost", "record", "standing", "?"]
    if any(trigger in message_lower for trigger in sports_triggers):
        if "?" in message_lower or any(cue in message_lower for cue in sports_cues):
            return {
                "needs_search": True,
                "query": message,
                "reason": "Sports/team query detected",
            }

    realtime_triggers = [
        "weather forecast",
        "current weather",
        "temperature outside",
        "latest news",
        "breaking news",
        "news about",
        "stock price",
        "exchange rate",
        "current time in",
        "what time is",
        "open now",
        "closing time",
        "current status",
        "update on",
        "any updates",
        "in stock",
        "available now",
    ]
    if any(trigger in message_lower for trigger in realtime_triggers):
        return {
            "needs_search": True,
            "query": message,
            "reason": "Real-time information query detected",
        }

    entertainment_keywords = [
        "movie",
        "movies",
        "film",
        "showtimes",
        "concert",
        "festival",
        "album",
        "music release",
        "series",
        "episodes",
        "game release",
        "tickets",
        "theater",
        "streaming",
    ]
    entertainment_cues = [
        "?",
        "what",
        "which",
        "new",
        "latest",
        "coming",
        "out",
        "release",
        "schedule",
    ]
    if any(kw in message_lower for kw in entertainment_keywords):
        if "?" in message_lower or any(
            cue in message_lower for cue in entertainment_cues
        ):
            return {
                "needs_search": True,
                "query": message,
                "reason": "Entertainment release or schedule query",
            }

    dining_keywords = [
        "restaurant",
        "restaurants",
        "diner",
        "bar",
        "cafe",
        "coffee shop",
        "food near",
        "dinner spot",
        "lunch spot",
        "where to eat",
        "menu",
        "reservations",
    ]
    dining_cues = [
        "?",
        "best",
        "good",
        "near",
        "close by",
        "open",
        "recommend",
        "suggest",
    ]
    if any(kw in message_lower for kw in dining_keywords):
        if "?" in message_lower or any(cue in message_lower for cue in dining_cues):
            return {
                "needs_search": True,
                "query": message,
                "reason": "Dining or venue lookup",
            }

    recommendation_triggers = ["recommend", "recommendation", "suggest", "suggestion"]
    recommendation_targets = [
        "restaurant",
        "bar",
        "cafe",
        "coffee",
        "movie",
        "film",
        "show",
        "book",
        "podcast",
        "game",
        "board game",
        "video game",
        "hotel",
        "vacation",
        "travel",
        "things to do",
        "activities",
        "museum",
        "event",
    ]
    if any(trigger in message_lower for trigger in recommendation_triggers):
        if any(target in message_lower for target in recommendation_targets):
            return {
                "needs_search": True,
                "query": message,
                "reason": "Recommendation request for venues or media",
            }

    return {
        "needs_search": False,
        "query": None,
        "reason": "Conversational wellness message",
    }


async def enhance_response_with_search_async(
    message: str,
    model: Optional[str] = None,
    use_llm_decision: bool = False,
    *,
    personality: Optional[str] = None,
    throttle_seconds: float = 5.0,
    cache_ttl: float = 300.0,
) -> Optional[str]:
    """
    Check if message needs search, perform search, return formatted results.

    This is the main entry point for web search integration.

    Args:
        message: User's message
        model: Optional model for LLM decision (defaults to settings().search_decision_model)
        use_llm_decision: Use LLM for smart detection (False uses keyword heuristics)
        personality: Current personality mode (skip search for downbad/roleplay)
        throttle_seconds: Minimum seconds between live web requests
        cache_ttl: Cache retention window in seconds for repeated queries

    Returns:
        Formatted search results string or None if no search needed

    Example:
        search_info = await enhance_response_with_search_async("How are the Cowboys?")
        if search_info:
            response += f"\\n\\n{search_info}"
    """
    try:
        global _LAST_SEARCH_AT

        # Skip if personality should not surface real-time info
        if personality and personality.lower() in {"downbad", "roleplay"}:
            return None

        normalized_message = (message or "").strip().lower()
        now = time.monotonic()
        cache_key = normalized_message

        cached_entry = _RESULT_CACHE.get(cache_key)
        if cached_entry:
            stored_at, cached_text = cached_entry
            if now - stored_at <= cache_ttl:
                _prune_cache(now, cache_ttl)
                return cached_text

        if now - _LAST_SEARCH_AT < throttle_seconds:
            return None

        # Decide if search is needed
        if use_llm_decision:
            decision = detect_search_need_with_llm(message, model)
        else:
            decision = should_search_web(message)

        if not decision["needs_search"]:
            return None

        search_query = decision["query"] or message
        cache_key = (search_query or "").strip().lower()
        if cache_key:
            cached_entry = _RESULT_CACHE.get(cache_key)
            if cached_entry:
                stored_at, cached_text = cached_entry
                if now - stored_at <= cache_ttl:
                    _prune_cache(now, cache_ttl)
                    return cached_text

        # Perform search
        try:
            results = await search_web_async(search_query, max_results=3, timeout=3)
        except WebSearchRateLimitError:
            return "Web search is cooling down. Please try again in a few seconds."
        _LAST_SEARCH_AT = time.monotonic()

        if not results:
            logger.info(f"[Web Search] No results for: {search_query}")
            _prune_cache(time.monotonic(), cache_ttl)
            return None
        # Format results
        lines = ["**Web Search Findings:**"]
        for result in results:
            title = (result.get("title") or "").strip() or "Untitled result"
            lines.append(f"- {title}")
            snippet = (result.get("body") or "").strip()
            if snippet:
                preview = snippet[:180] + ("..." if len(snippet) > 180 else "")
                lines.append(f"  {preview}")
            url = (result.get("href") or "").strip()
            if url:
                lines.append(f"  {url}")
        lines.append("")
        search_text = "\n".join(lines).strip()
        stored_at = time.monotonic()
        if cache_key:
            _RESULT_CACHE[cache_key] = (stored_at, search_text)
        if cache_key != normalized_message:
            _RESULT_CACHE[normalized_message] = (stored_at, search_text)
        _prune_cache(stored_at, cache_ttl)

        logger.info(f"[Web Search] Found {len(results)} results for: {search_query}")
        return search_text

    except Exception as e:
        logger.error(f"Search enhancement error: {e}")
        return None


# Synchronous wrapper for non-async contexts
def enhance_response_with_search(
    message: str,
    model: Optional[str] = None,
    use_llm_decision: bool = False,
    *,
    personality: Optional[str] = None,
    throttle_seconds: float = 5.0,
    cache_ttl: float = 300.0,
) -> Optional[str]:
    """
    Synchronous version of enhance_response_with_search_async.

    Creates event loop if needed and runs async version.
    Use this in non-async code.
    Passes through personality/throttle/cache hints to the async helper.
    """
    try:
        # Get or create event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Run async version
        return loop.run_until_complete(
            enhance_response_with_search_async(
                message,
                model,
                use_llm_decision,
                personality=personality,
                throttle_seconds=throttle_seconds,
                cache_ttl=cache_ttl,
            )
        )
    except Exception as e:
        logger.error(f"Sync search enhancement error: {e}")
        return None


# #todo: Add search result caching to prevent duplicate searches
# #todo: Add source credibility scoring
# #todo: Support multiple search engines (Google, Bing as fallbacks)
# #todo: Add search history tracking per user
# #todo: Implement smart query expansion for ambiguous queries
