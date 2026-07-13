"""The seed ontology — the controlled vocabulary the graph is built from.

This is intentionally data, not code: extending the ontology means adding an
entry here (or an INSERT at runtime), never an ENUM migration. The extraction
engine in Step 2 is constrained to emit only these node/relationship types.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from app.db.models.ontology import OntologyNodeType, OntologyRelationshipType

# --- Entity types ----------------------------------------------------------
NODE_TYPES: list[dict] = [
    {"name": "user", "description": "A person (GitHub/Slack identity)."},
    {"name": "team", "description": "A group of users / org unit."},
    {"name": "repository", "description": "A source code repository."},
    {"name": "pull_request", "description": "A GitHub pull request."},
    {"name": "issue", "description": "A GitHub issue or ticket."},
    {"name": "commit", "description": "A git commit."},
    {"name": "file", "description": "A file / module within a repository."},
    {"name": "feature", "description": "A product capability or feature area (often inferred)."},
    {"name": "service", "description": "A deployable service or component."},
    {"name": "deployment", "description": "A release/deploy event of a service or repo."},
    {"name": "incident", "description": "An outage or deployment failure."},
    {"name": "slack_channel", "description": "A Slack channel."},
    {"name": "slack_thread", "description": "A Slack conversation thread."},
    {"name": "slack_message", "description": "A single Slack message."},
    {"name": "document", "description": "A Notion page or other generic document."},
    {"name": "meeting", "description": "A call (Zoom/Meet/Teams) with a transcript."},
    {"name": "participant", "description": "A person who took part in a meeting."},
]

# --- Relationship types ----------------------------------------------------
# allowed_*_types == None means "any". These constraints are advisory and
# enforced at the application/extraction layer; the FK only guarantees the
# relationship term itself is known.
RELATIONSHIP_TYPES: list[dict] = [
    {
        "name": "AUTHORED",
        "description": "Actor created an artifact.",
        "allowed_source_types": ["user"],
        "allowed_target_types": ["pull_request", "issue", "commit", "slack_message", "document"],
    },
    {
        "name": "REVIEWED",
        "description": "User reviewed a pull request.",
        "allowed_source_types": ["user"],
        "allowed_target_types": ["pull_request"],
    },
    {
        "name": "MEMBER_OF",
        "description": "User belongs to a team.",
        "allowed_source_types": ["user"],
        "allowed_target_types": ["team"],
    },
    {
        "name": "OWNS",
        "description": "Team owns a repository or service.",
        "allowed_source_types": ["team"],
        "allowed_target_types": ["repository", "service"],
    },
    {
        "name": "PART_OF",
        "description": "Artifact belongs to a larger container.",
        "allowed_source_types": None,
        "allowed_target_types": None,
    },
    {
        "name": "MODIFIES",
        "description": "A change touches a file/feature.",
        "allowed_source_types": ["pull_request", "commit"],
        "allowed_target_types": ["file", "feature", "service"],
    },
    {
        "name": "IMPLEMENTS",
        "description": "A change implements a feature.",
        "allowed_source_types": ["pull_request", "commit"],
        "allowed_target_types": ["feature"],
    },
    {
        "name": "DISCUSSES",
        "description": "A conversation/meeting is about a graph entity.",
        "allowed_source_types": ["slack_thread", "slack_message", "document", "meeting"],
        "allowed_target_types": ["feature", "pull_request", "incident", "service", "repository"],
    },
    {
        "name": "PARTICIPATES_IN",
        "description": "A person took part in a meeting.",
        "allowed_source_types": ["participant", "user"],
        "allowed_target_types": ["meeting"],
    },
    {
        "name": "REFERENCES",
        "description": "A message/doc links to an artifact.",
        "allowed_source_types": None,
        "allowed_target_types": None,
    },
    {
        "name": "MENTIONS",
        "description": "Generic surface-level mention.",
        "allowed_source_types": None,
        "allowed_target_types": None,
    },
    {
        "name": "CAUSED",
        "description": "A change/feature caused an incident.",
        "allowed_source_types": ["pull_request", "commit", "feature", "deployment"],
        "allowed_target_types": ["incident"],
    },
    {
        "name": "RESOLVES",
        "description": "A change resolves an incident/issue.",
        "allowed_source_types": ["pull_request", "commit"],
        "allowed_target_types": ["incident", "issue"],
    },
    {
        "name": "TARGETS",
        "description": "A deployment targets a repo/service.",
        "allowed_source_types": ["deployment"],
        "allowed_target_types": ["repository", "service"],
    },
    {
        "name": "TRIGGERED_BY",
        "description": "A deployment was triggered by a change.",
        "allowed_source_types": ["deployment"],
        "allowed_target_types": ["pull_request", "commit"],
    },
    {
        "name": "AFFECTS",
        "description": "An incident affects a service/repo.",
        "allowed_source_types": ["incident"],
        "allowed_target_types": ["service", "repository", "feature"],
    },
    {
        "name": "POSTED_IN",
        "description": "A message/thread lives in a channel/thread.",
        "allowed_source_types": ["slack_message", "slack_thread"],
        "allowed_target_types": ["slack_channel", "slack_thread"],
    },
    {
        "name": "DEPENDS_ON",
        "description": "Symmetric dependency between components.",
        "allowed_source_types": ["service", "feature"],
        "allowed_target_types": ["service", "feature"],
        "is_symmetric": True,
    },
    {
        "name": "RELATES_TO",
        "description": "Generic association.",
        "allowed_source_types": None,
        "allowed_target_types": None,
        "is_symmetric": True,
    },
]


def seed_ontology(engine: Engine) -> None:
    """Idempotently upsert the ontology registry. Safe to run on every boot."""
    with engine.begin() as conn:
        for nt in NODE_TYPES:
            stmt = insert(OntologyNodeType).values(
                name=nt["name"],
                description=nt.get("description"),
                properties_schema=nt.get("properties_schema"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "description": stmt.excluded.description,
                    "properties_schema": stmt.excluded.properties_schema,
                },
            )
            conn.execute(stmt)

        for rt in RELATIONSHIP_TYPES:
            stmt = insert(OntologyRelationshipType).values(
                name=rt["name"],
                description=rt.get("description"),
                allowed_source_types=rt.get("allowed_source_types"),
                allowed_target_types=rt.get("allowed_target_types"),
                is_symmetric=rt.get("is_symmetric", False),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "description": stmt.excluded.description,
                    "allowed_source_types": stmt.excluded.allowed_source_types,
                    "allowed_target_types": stmt.excluded.allowed_target_types,
                    "is_symmetric": stmt.excluded.is_symmetric,
                },
            )
            conn.execute(stmt)
