"""The ontology registry — the schema OF the graph, stored as data.

Rather than encoding node/edge types as Postgres ENUMs (which require an
`ALTER TYPE` migration to evolve) we keep them as rows in two registry tables.
`nodes.node_type` and `edges.relationship` carry foreign keys into these
tables, so the database enforces that every node/edge uses a *known* ontology
term, while extending the ontology is a plain INSERT.
"""

from sqlalchemy import Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin


class OntologyNodeType(TimestampMixin, Base):
    __tablename__ = "ontology_node_types"

    # `name` is the natural primary key, referenced by nodes.node_type.
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    # Optional JSON Schema used to validate `nodes.properties` for this type.
    properties_schema: Mapped[dict | None] = mapped_column(JSONB)


class OntologyRelationshipType(TimestampMixin, Base):
    __tablename__ = "ontology_relationship_types"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    # NULL == "any node type allowed". Otherwise a JSON array of node type
    # names constraining valid endpoints (enforced at the application layer).
    allowed_source_types: Mapped[list | None] = mapped_column(JSONB)
    allowed_target_types: Mapped[list | None] = mapped_column(JSONB)
    # Symmetric relationships (e.g. DEPENDS_ON between services) can be traversed
    # in either direction by the query layer.
    is_symmetric: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
