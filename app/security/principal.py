"""Principal & grants — the identity/authorization value objects.

A Principal carries who the caller is (subject, groups) and what they may read
(grants). Grants are anchored on (source_system, resource_key) — the same pair
recorded on every raw_documents row — so authorization is a provenance check.
"""

from dataclasses import dataclass, field
from hashlib import sha256


@dataclass(frozen=True)
class SourceGrant:
    source_system: str
    # None == every resource in this source_system (e.g. all GitHub repos).
    resources: frozenset[str] | None = None


@dataclass(frozen=True)
class Principal:
    subject: str
    groups: tuple[str, ...] = ()
    grants: tuple[SourceGrant, ...] = field(default=())
    superuser: bool = False

    @classmethod
    def anonymous(cls) -> "Principal":
        """Unrestricted principal used when RBAC is disabled (back-compat)."""
        return cls(subject="anonymous", superuser=True)

    def scope_key(self) -> str:
        """Stable short hash of the *visible scope*. Part of every cache key so
        two principals with different visibility never share cached results."""
        if self.superuser:
            return "su"
        material = ";".join(
            sorted(
                f"{g.source_system}:{'*' if g.resources is None else ','.join(sorted(g.resources))}"
                for g in self.grants
            )
        )
        return sha256(material.encode()).hexdigest()[:16]
