"""Proposal layer — the single mutation choke point for self-maintenance.

Importing this package registers all built-in handlers (mirrors how
`app.connectors` imports its drivers so `@ConnectorFactory.register` runs).
"""

from app.proposals import merge  # noqa: F401  (registers entity_merge)
from app.proposals.engine import ProposalEngine, ProposalError, get_handler, register_handler
from app.proposals.merge import merge_dedup_key

__all__ = [
    "ProposalEngine",
    "ProposalError",
    "get_handler",
    "register_handler",
    "merge_dedup_key",
]
