"""
RAG (Retrieval-Augmented Generation) Module

Mission Statement:
This module implements RAG for the Personal Wellness Companion Bot.
The goal is to provide users with accurate, evidence-based wellness information
by retrieving relevant documents from a vector database and using them to
augment LLM responses.

Key Components:
- vector_store.py: Manages vector database operations (sqlite-vec)
- ingestion.py: Processes and chunks wellness documents
- retrieval.py: Searches and ranks relevant documents

Dependencies:
- app.vector_backends: Vector storage abstraction
- app.utils.ollama: Embedding generation
- app.config: Configuration management
"""

from .ingestion import ResourceIngester
from .retrieval import WellnessRetriever
from .service import format_citations, get_retriever, get_vector_store
from .vector_store import WellnessVectorStore

__all__ = [
    "WellnessVectorStore",
    "ResourceIngester",
    "WellnessRetriever",
    "get_vector_store",
    "get_retriever",
    "format_citations",
]
