"""Review queue CLI — the human side of the proposal loop.

    python -m app.review list [--status pending|applied|...] [--kind entity_merge]
    python -m app.review show     <id-or-prefix>
    python -m app.review approve  <id-or-prefix> [--by NAME]
    python -m app.review reject   <id-or-prefix> [--by NAME]
    python -m app.review rollback <id-or-prefix> [--by NAME]

Ids accept unique prefixes (the first 8 chars `list` prints are enough).
"""

import getpass
import json
import sys

from sqlalchemy import cast, select
from sqlalchemy.types import Text as SAText

from app.db.models.proposal import Proposal
from app.db.session import SessionLocal
from app.proposals import ProposalEngine, ProposalError

_STATUSES = ["pending", "applied", "auto_applied", "rejected", "rolled_back", "failed"]


def _summary(p: Proposal) -> str:
    if p.kind == "entity_merge":
        ev = p.evidence or {}
        loser = ev.get("loser", {}).get("name", p.payload.get("loser_id", "?"))
        winner = ev.get("winner", {}).get("name", p.payload.get("winner_id", "?"))
        return f"merge '{loser}' -> '{winner}'"
    if p.kind == "schema_node_type":
        return f"add node type '{p.payload.get('name')}'"
    if p.kind == "schema_relationship_type":
        return f"add relationship '{p.payload.get('name')}'"
    if p.kind == "stale_flag":
        return f"flag stale: {p.payload.get('reason', p.payload.get('node_id'))}"
    return json.dumps(p.payload)[:60]


def _find(db, prefix: str) -> Proposal:
    rows = db.scalars(
        select(Proposal).where(cast(Proposal.id, SAText).like(f"{prefix.lower()}%"))
    ).all()
    if not rows:
        raise ProposalError(f"No proposal matches '{prefix}'.")
    if len(rows) > 1:
        raise ProposalError(f"'{prefix}' is ambiguous ({len(rows)} matches) — use more chars.")
    return rows[0]


def cmd_list(db, status: str | None, kind: str | None) -> None:
    stmt = select(Proposal).order_by(Proposal.confidence.desc(), Proposal.created_at)
    if status:
        stmt = stmt.where(Proposal.status == status)
    if kind:
        stmt = stmt.where(Proposal.kind == kind)
    rows = db.scalars(stmt).all()
    if not rows:
        print(f"Queue is clean — no {status or 'matching'} proposals.")
        return
    print(f"{'ID':<10}{'KIND':<14}{'CONF':<7}{'AGENT':<8}{'STATUS':<13}SUMMARY")
    for p in rows:
        print(
            f"{str(p.id)[:8]:<10}{p.kind:<14}{p.confidence:<7.2f}"
            f"{p.agent:<8}{p.status:<13}{_summary(p)}"
        )
    print(f"\n{len(rows)} proposal(s).")


def cmd_show(db, prefix: str) -> None:
    p = _find(db, prefix)
    print(f"PROPOSAL {p.id}")
    print(f"  kind       : {p.kind}")
    print(f"  status     : {p.status}")
    print(f"  confidence : {p.confidence:.3f}")
    print(f"  agent      : {p.agent}")
    print(f"  summary    : {_summary(p)}")
    print(f"  filed      : {p.created_at:%Y-%m-%d %H:%M}")
    if p.reviewed_by:
        print(f"  reviewed   : by {p.reviewed_by} at {p.decided_at or p.updated_at:%Y-%m-%d %H:%M}")
    if p.error:
        print(f"  error      : {p.error}")
    print(f"  payload    : {json.dumps(p.payload, indent=2)}")
    if p.evidence:
        print(f"  evidence   : {json.dumps(p.evidence, indent=2)}")
    if p.rollback_data is not None:
        print("  rollback   : snapshot available — `rollback` restores the prior graph")


def _decide(db, prefix: str, action: str, by: str) -> None:
    p = _find(db, prefix)
    engine = ProposalEngine(db)
    p = getattr(engine, action)(p.id, reviewed_by=by)
    print(f"ok: {str(p.id)[:8]} ({p.kind}) -> {p.status}")


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.review", description="Review the self-maintenance proposal queue."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Show the queue (highest confidence first).")
    p_list.add_argument("--status", choices=_STATUSES, default="pending")
    p_list.add_argument("--all", action="store_true", help="Every status, not just pending.")
    p_list.add_argument("--kind")
    p_show = sub.add_parser("show", help="Full detail for one proposal.")
    p_show.add_argument("id")
    for name, help_text in (
        ("approve", "Apply a pending proposal to the graph."),
        ("reject", "Refuse a pending proposal (sticky — never re-filed)."),
        ("rollback", "Undo an applied proposal from its snapshot."),
    ):
        p_cmd = sub.add_parser(name, help=help_text)
        p_cmd.add_argument("id")
        p_cmd.add_argument("--by", default=getpass.getuser())
    args = parser.parse_args()

    with SessionLocal() as db:
        try:
            if args.cmd == "list":
                cmd_list(db, None if args.all else args.status, args.kind)
            elif args.cmd == "show":
                cmd_show(db, args.id)
            else:
                _decide(db, args.id, args.cmd, args.by)
        except ProposalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
