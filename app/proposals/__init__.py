"""Proposal layer — the single mutation choke point for self-maintenance.

Importing this package registers all built-in handlers (mirrors how
`app.connectors` imports its drivers so `@ConnectorFactory.register` runs).
"""

from app.proposals import (  # noqa: F401  (register handlers)
    conflict,
    edge_add,
    hygiene,
    merge,
    schema_types,
    stale,
)
from app.proposals.engine import ProposalEngine, ProposalError, get_handler, register_handler
from app.proposals.merge import merge_dedup_key
from app.proposals.schema_types import file_unmapped

__all__ = [
    "ProposalEngine",
    "ProposalError",
    "get_handler",
    "register_handler",
    "merge_dedup_key",
    "file_unmapped",
]
