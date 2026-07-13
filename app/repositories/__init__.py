"""Data-access layer. All graph reads flow through GraphRepository, the single
choke point for row-level visibility (Track 1) and bounded traversal (Track 3)."""

from app.repositories.graph import GraphRepository

__all__ = ["GraphRepository"]
