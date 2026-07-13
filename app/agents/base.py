"""Maintenance agents — the scanners that keep the graph groomed.

An agent inspects the graph for one kind of decay (duplicates, schema drift,
stale facts) and files typed proposals through the `ProposalEngine`. Agents
never mutate the graph directly — the proposal queue is the only write path,
so every change is reviewable, audited, and reversible.

Agents self-register with `@AgentFactory.register("name")`, mirroring
`ConnectorFactory`: the runner CLI discovers them through the registry.
"""

from sqlalchemy.orm import Session

from app.db.models.proposal import Proposal
from app.proposals import ProposalEngine


class MaintenanceAgent:
    name: str = "base"

    def __init__(self, db: Session):
        self.db = db
        self.engine = ProposalEngine(db)

    def scan(self) -> list[Proposal]:
        """Inspect the graph and file proposals. Returns the *newly filed* rows
        (submissions deduplicated away by an earlier run are not repeated)."""
        raise NotImplementedError


class AgentFactory:
    _registry: dict[str, type[MaintenanceAgent]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(klass: type[MaintenanceAgent]) -> type[MaintenanceAgent]:
            klass.name = name
            cls._registry[name] = klass
            return klass

        return decorator

    @classmethod
    def create(cls, db: Session, name: str) -> MaintenanceAgent:
        if name not in cls._registry:
            raise ValueError(f"Unknown agent {name!r}. Available: {cls.available()}")
        return cls._registry[name](db)

    @classmethod
    def available(cls) -> list[str]:
        return sorted(cls._registry)
