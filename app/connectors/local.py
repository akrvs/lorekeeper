"""Local / Obsidian connector (Track 4) — parallelized filesystem scanning.

Scans a directory tree for Markdown/text files, reading them concurrently with a
bounded thread pool (file I/O is blocking, so it's offloaded). Obsidian
`[[wikilinks]]` are surfaced in `raw_payload` for the ontology engine to resolve
into REFERENCES edges between notes.
"""

import asyncio
import re
import uuid
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory
from app.db.models import Edge, Node

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def slugify(value: str) -> str:
    """Normalize a file name or wikilink target to a comparable slug.

    'deploy-process.md', 'Deploy Process', and '[[deploy_process]]' all → 'deploy-process'.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.lower().removesuffix(".md")).strip("-")


@ConnectorFactory.register("local")
class LocalConnector(BaseConnector):
    source_system = "local"

    def __init__(
        self,
        db: Session,
        *,
        root: str | None = None,
        patterns: tuple[str, ...] = ("*.md", "*.txt", "*.markdown"),
        max_items: int | None = None,
        **_,
    ):
        super().__init__(db)
        self.root = Path(root or settings.local_root or ".").expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"LocalConnector root is not a directory: {self.root}")
        self.resource_key = self.root.name
        self.patterns = patterns
        self.max_items = max_items or settings.ingest_max_items
        self._concurrency = settings.local_scan_concurrency

    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    async def _fetch_all(self) -> list[RawDoc]:
        files = sorted(
            {p for pattern in self.patterns for p in self.root.rglob(pattern) if p.is_file()}
        )[: self.max_items]
        sem = asyncio.Semaphore(self._concurrency)

        async def read(path: Path) -> RawDoc:
            async with sem:  # bound the number of concurrent file reads
                body = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="ignore")
            rel = path.relative_to(self.root).as_posix()
            return RawDoc(
                source_type="note",
                external_id=rel,
                resource_key=self.root.name,  # the vault / directory is the ACL unit
                title=path.stem,
                url=path.as_uri(),
                author=None,
                content=body,
                raw_payload={"path": rel, "wikilinks": sorted(set(_WIKILINK.findall(body)))},
            )

        return list(await asyncio.gather(*(read(p) for p in files)))


# --------------------------------------------------------------------------- #
# Wikilink resolution (Phase 9)
# --------------------------------------------------------------------------- #
def reconcile_wikilinks(db: Session) -> int:
    """Reconcile derived `[[wikilink]]` concept nodes against the ingested file
    pool: when a concept's slug matches a `local` file node, merge the concept
    INTO that file node (redirect its edges + provenance, then delete it) so the
    reference points at the real file — not a detached abstract concept.

    Returns the number of concept nodes resolved into file nodes.
    """
    files = db.execute(
        select(Node.id, Node.external_id, Node.name).where(Node.source_system == "local")
    ).all()
    slug_to_file: dict[str, uuid.UUID] = {}
    for file_id, external_id, name in files:
        for candidate in (external_id, name):
            if candidate:
                slug_to_file.setdefault(slugify(candidate), file_id)

    # Only consider concept nodes actually referenced by a local file.
    local_file = aliased(Node)
    concepts = db.execute(
        select(Node.id, Node.name)
        .distinct()
        .join(Edge, (Edge.target_id == Node.id) & (Edge.relationship_type == "REFERENCES"))
        .join(local_file, (local_file.id == Edge.source_id) & (local_file.source_system == "local"))
        .where(Node.source_system.is_(None), Node.node_type == "document")
    ).all()

    resolved = 0
    for concept_id, concept_name in concepts:
        file_id = slug_to_file.get(slugify(concept_name))
        if file_id and file_id != concept_id:
            _merge_concept_into_file(db, concept_id, file_id)
            resolved += 1
    db.commit()
    return resolved


def _merge_concept_into_file(db: Session, concept_id: uuid.UUID, file_id: uuid.UUID) -> None:
    params = {"cid": concept_id, "fid": file_id}
    # Redirect edges that point AT the concept to the file node.
    db.execute(
        text(
            "INSERT INTO edges "
            "(source_id, target_id, relationship, properties, weight, confidence) "
            "SELECT source_id, :fid, relationship, properties, weight, confidence "
            "FROM edges WHERE target_id = :cid AND source_id <> :fid "
            "ON CONFLICT ON CONSTRAINT uq_edges_identity DO NOTHING"
        ),
        params,
    )
    # Redirect edges that originate FROM the concept.
    db.execute(
        text(
            "INSERT INTO edges "
            "(source_id, target_id, relationship, properties, weight, confidence) "
            "SELECT :fid, target_id, relationship, properties, weight, confidence "
            "FROM edges WHERE source_id = :cid AND target_id <> :fid "
            "ON CONFLICT ON CONSTRAINT uq_edges_identity DO NOTHING"
        ),
        params,
    )
    # Carry over provenance (which documents mentioned the concept).
    db.execute(
        text(
            "INSERT INTO node_mentions (node_id, document_id, context) "
            "SELECT :fid, document_id, context FROM node_mentions WHERE node_id = :cid "
            "ON CONFLICT ON CONSTRAINT uq_node_mentions_identity DO NOTHING"
        ),
        params,
    )
    # Delete the now-merged concept (cascades its remaining edges + mentions).
    db.execute(text("DELETE FROM nodes WHERE id = :cid"), params)
