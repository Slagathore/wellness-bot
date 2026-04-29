"""
Wellness Resource Retrieval Module

Mission Statement:
This module handles intelligent retrieval of wellness resources
for user queries. It integrates with the vector store to find
relevant information and formats it for LLM augmentation.

Features:
- Query understanding (extract intent, keywords)
- Semantic search with re-ranking
- Context-aware filtering
- Citation formatting
- Query result caching for faster repeated lookups

#todo: Add query expansion (synonyms, related terms)
#todo: Implement hybrid search (vector + keyword)
#todo: Add conversational context to improve retrieval
#todo: Track retrieval quality metrics
"""

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

from .vector_store import WellnessVectorStore

logger = logging.getLogger(__name__)


class WellnessRetriever:
    """Retrieve relevant wellness resources for queries"""

    # Query patterns that indicate information-seeking
    INFO_PATTERNS = [
        "what is",
        "how to",
        "how do i",
        "tell me about",
        "explain",
        "help with",
        "tips for",
        "ways to",
        "strategies",
        "techniques",
        "resources for",
        "information on",
        "learn about",
        "understand",
        "deal with",
        "manage",
        "cope with",
        "handle",
    ]

    def __init__(self, vector_store: WellnessVectorStore):
        """
        Initialize retriever

        Args:
            vector_store: WellnessVectorStore instance
        """
        self.vector_store = vector_store
        # Cache for retrieval results (query_hash -> result dict)
        # Stores last 100 queries with TTL of 1 hour
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_max_size = 100
        self._cache_ttl = 3600  # 1 hour in seconds

    def should_retrieve(self, query: str) -> bool:
        """
        Determine if query warrants resource retrieval

        CRITICAL: Only trigger for wellness-specific information requests.
        Do NOT trigger for casual conversation, personal questions, or emotional venting.

        Args:
            query: User query

        Returns:
            True if query appears to be seeking wellness information/advice
        """
        query_lower = query.lower()

        # Wellness-specific keywords that indicate information seeking
        wellness_keywords = [
            "sleep",
            "insomnia",
            "anxiety",
            "depression",
            "stress",
            "panic",
            "meditation",
            "mindfulness",
            "therapy",
            "counseling",
            "exercise",
            "nutrition",
            "diet",
            "mental health",
            "wellness",
            "coping",
            "breathing",
            "relaxation",
            "self-care",
            "burnout",
            "mood",
            "emotional",
            "ptsd",
            "trauma",
            "grief",
            "adhd",
            "ocd",
            "bipolar",
            "technique",
            "strategy",
            "improve",
            "better",
            "manage",
            "deal with",
            "overcome",
            "reduce",
            "helpful",
            "effective",
            "recommend",
            "suggestion",
        ]

        # Check if query contains wellness keywords + info-seeking patterns
        has_wellness_keyword = any(kw in query_lower for kw in wellness_keywords)

        if not has_wellness_keyword:
            # No wellness context - don't retrieve
            return False

        # Now check if it's information-seeking (not just mentioning the topic)
        for pattern in self.INFO_PATTERNS:
            if pattern in query_lower:
                return True

        # Check for question words at start ("how do I...", "what is...")
        question_words = ["what", "how", "why", "when", "where", "which"]
        if any(query_lower.startswith(word) for word in question_words):
            return True

        # Check if query ends with question mark AND mentions wellness topic
        if query.strip().endswith("?"):
            return True

        return False

    def retrieve(
        self, query: str, top_k: int = 3, category: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Retrieve relevant wellness resources (with caching)

        Args:
            query: User query
            top_k: Number of resources to retrieve
            category: Optional category filter

        Returns:
            Dict with:
                - context: Formatted context for LLM
                - sources: List of source citations
                - resources: Raw search results
        """
        # Generate cache key from query params
        cache_key = self._get_cache_key(query, top_k, category)

        # Check cache first
        cached_result = self._get_from_cache(cache_key)
        if cached_result is not None:
            logger.info(f"[RAG Cache] Hit for query: {query[:50]}...")
            return cached_result

        logger.info(f"[RAG Cache] Miss for query: {query[:50]}...")

        # Search vector store
        results = self.vector_store.search(query, top_k=top_k, category=category)

        if not results:
            empty_result = {"context": "", "sources": [], "resources": []}
            self._add_to_cache(cache_key, empty_result)
            return empty_result

        # Format context for LLM
        context = self._format_context(results)

        # Generate citations
        sources = self._generate_citations(results)

        result = {"context": context, "sources": sources, "resources": results}

        # Cache the result
        self._add_to_cache(cache_key, result)

        return result

    def _get_cache_key(self, query: str, top_k: int, category: Optional[str]) -> str:
        """
        Generate cache key from query parameters

        Args:
            query: User query
            top_k: Number of results
            category: Category filter

        Returns:
            Hash string for cache key
        """
        # Normalize query (lowercase, strip whitespace)
        normalized_query = query.lower().strip()

        # Create key from params
        key_str = f"{normalized_query}|{top_k}|{category or 'none'}"

        # Hash for consistent key
        return hashlib.md5(key_str.encode()).hexdigest()

    def _get_from_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        Get cached result if available and not expired

        Args:
            cache_key: Cache key hash

        Returns:
            Cached result or None
        """
        if cache_key not in self._cache:
            return None

        # Check if expired
        timestamp = self._cache_timestamps.get(cache_key, 0)
        if time.time() - timestamp > self._cache_ttl:
            # Expired, remove from cache
            del self._cache[cache_key]
            del self._cache_timestamps[cache_key]
            return None

        return self._cache[cache_key]

    def _add_to_cache(self, cache_key: str, result: Dict[str, Any]) -> None:
        """
        Add result to cache with LRU eviction

        Args:
            cache_key: Cache key hash
            result: Result to cache
        """
        # Evict oldest if at max size
        if len(self._cache) >= self._cache_max_size:
            # Find oldest entry
            oldest_key = min(
                self._cache_timestamps.keys(), key=lambda k: self._cache_timestamps[k]
            )
            del self._cache[oldest_key]
            del self._cache_timestamps[oldest_key]

        # Add new entry
        self._cache[cache_key] = result
        self._cache_timestamps[cache_key] = time.time()

    def clear_cache(self) -> None:
        """Clear all cached retrieval results"""
        self._cache.clear()
        self._cache_timestamps.clear()
        logger.info("[RAG Cache] Cleared all cached results")

    def _format_context(self, results: List[Dict[str, Any]]) -> str:
        """
        Format search results into LLM context

        Args:
            results: Search results from vector store

        Returns:
            Formatted context string
        """
        if not results:
            return ""

        context_parts = ["**Relevant Wellness Resources:**\n"]

        for idx, result in enumerate(results, 1):
            context_parts.append(f"\n**Resource {idx}: {result['title']}**")
            if result.get("source"):
                context_parts.append(f"(Source: {result['source']})")
            context_parts.append(f"\n{result['chunk_text']}\n")

        context_parts.append("\n**Instructions:**")
        context_parts.append(
            "Use the above resources to provide accurate, evidence-based guidance."
        )
        context_parts.append(
            "Cite sources when providing specific techniques or facts."
        )
        context_parts.append(
            "If resources don't fully answer the question, acknowledge limitations."
        )

        return "\n".join(context_parts)

    def _generate_citations(self, results: List[Dict[str, Any]]) -> List[str]:
        """
        Generate citation list

        Args:
            results: Search results

        Returns:
            List of citation strings
        """
        citations = []

        for result in results:
            citation = f"📚 {result['title']}"

            if result.get("source"):
                citation += f" ({result['source']})"

            if result.get("url"):
                citation += f"\n   {result['url']}"

            citations.append(citation)

        return citations

    def retrieve_by_category(
        self, category: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Get top resources for a specific category

        Args:
            category: Wellness category (anxiety, depression, sleep, etc.)
            top_k: Number of resources to return

        Returns:
            List of resources
        """
        # Use a generic query for the category
        query = f"Information about {category} and how to manage it"

        return self.vector_store.search(query, top_k=top_k, category=category)

    def get_related_resources(
        self, doc_id: str, top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Find resources related to a specific document

        Args:
            doc_id: Document ID
            top_k: Number of related resources

        Returns:
            List of related resources
        """
        # This is a simplified version - in production, you'd use the document's
        # content or embedding to find similar documents

        # For now, just search by the document's category
        # (This would need access to document metadata)

        logger.warning("get_related_resources not fully implemented")
        return []


def create_rag_prompt(
    query: str, context: str, system_prompt: str = ""
) -> List[Dict[str, str]]:
    """
    Create RAG-augmented prompt for LLM

    Args:
        query: User query
        context: Retrieved context from vector store
        system_prompt: Optional system prompt

    Returns:
        List of message dicts for LLM
    """
    if not context:
        # No RAG context, use normal prompt
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

    # Build RAG prompt
    augmented_query = f"""{context}

---

**User Question:** {query}

**Remember:** Provide empathetic, personalized support while incorporating relevant information from the resources above. Cite sources when appropriate."""

    messages = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": augmented_query})

    return messages


# Module-level documentation:
# - WellnessRetriever: Main retrieval class with caching
# - should_retrieve(): Determine if query needs RAG
# - retrieve(): Semantic search with formatting and caching
# - create_rag_prompt(): Build augmented LLM prompt
# - _format_context(): Format search results for LLM
# - _generate_citations(): Create source citations
# - _get_cache_key(): Generate cache key from query params
# - _get_from_cache(): Retrieve cached results
# - _add_to_cache(): Store results with LRU eviction
# - clear_cache(): Clear all cached results

# Cache details:
# - Max size: 100 queries
# - TTL: 1 hour
# - Key: MD5 hash of (normalized_query|top_k|category)
# - Eviction: LRU when at max size

# #todo: Add query expansion with synonyms
# #todo: Implement hybrid search (dense + sparse)
# #todo: Add user feedback loop (mark helpful resources)
# #todo: Context-aware retrieval (use conversation history)
# #todo: Add cache statistics/metrics endpoint
