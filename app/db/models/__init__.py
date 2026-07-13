"""Import every model so `Base.metadata` is fully populated for migrations."""

from app.db.models.edge import Edge
from app.db.models.mention import NodeMention
from app.db.models.node import Node
from app.db.models.ontology import OntologyNodeType, OntologyRelationshipType
from app.db.models.proposal import Proposal
from app.db.models.security import AccessGrant, AuditLog
from app.db.models.source import IngestionRun, RawDocument

__all__ = [
    "OntologyNodeType",
    "OntologyRelationshipType",
    "RawDocument",
    "IngestionRun",
    "Node",
    "Edge",
    "NodeMention",
    "AccessGrant",
    "AuditLog",
    "Proposal",
]
