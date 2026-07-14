"""Interactive review TUI — the proposal queue as a split-pane curses app.

    python -m app.review tui

Top pane: the queue for one status filter, highest confidence first. Bottom
pane: full detail for the selected proposal — payload, evidence (including the
dedup judge's verdict + rationale), errors, rollback availability. Decisions
are one keystroke plus a y/N confirmation, and every action re-reads the queue
so the screen never lies about the database.

    ↑/↓ j/k  move          f        cycle status filter    a  approve
    u/d      scroll detail R        refresh from the DB    r  reject
    g/G      first/last    q / ESC  quit                   b  rollback

Stdlib curses only — no new dependency for the demo path. All queue logic
(`_detail_lines`, filters, actions) is plain data-in/data-out so it stays
testable without a terminal.
"""

import curses
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.proposal import Proposal
from app.db.session import SessionLocal
from app.proposals import ProposalEngine, ProposalError
from app.review import default_reviewer, summarize

# Last entry (None) means "every status".
FILTERS = ["pending", "applied", "auto_applied", "rejected", "rolled_back", "failed", None]

# key -> (engine verb, statuses it applies to)
ACTIONS = {
    ord("a"): ("approve", {"pending"}),
    ord("r"): ("reject", {"pending"}),
    ord("b"): ("rollback", {"applied", "auto_applied"}),
}

_HELP = "j/k move · u/d detail · f filter · a approve · r reject · b rollback · R refresh · q quit"

_STATUS_COLOR = {
    "pending": 1,  # yellow — needs a human
    "applied": 2,  # green — landed
    "auto_applied": 2,
    "rejected": 3,  # red — refused / undone
    "rolled_back": 3,
    "failed": 4,  # magenta — agent misbehavior, look closer
}


def load_queue(db: Session, status: str | None) -> list[Proposal]:
    stmt = select(Proposal).order_by(Proposal.confidence.desc(), Proposal.created_at)
    if status is not None:
        stmt = stmt.where(Proposal.status == status)
    return list(db.scalars(stmt).all())


def row_text(p: Proposal) -> str:
    return (
        f"{str(p.id)[:8]:<10}{p.kind:<24}{p.confidence:<7.2f}"
        f"{p.agent:<11}{p.status:<13}{summarize(p)}"
    )


def _detail_lines(p: Proposal) -> list[str]:
    lines = [
        f"proposal   {p.id}",
        f"kind       {p.kind} · confidence {p.confidence:.3f} · agent {p.agent} · {p.status}",
        f"summary    {summarize(p)}",
        f"filed      {p.created_at:%Y-%m-%d %H:%M}",
    ]
    judgment = (p.evidence or {}).get("llm_judgment")
    if judgment:  # JSONB reorders keys, so the verdict gets its own above-the-fold line
        lines.append(
            f"judge      {judgment.get('verdict', '?')} "
            f"({judgment.get('confidence', 0.0):.2f}) — {judgment.get('rationale', '')}"
        )
    if p.reviewed_by:
        when = p.decided_at or p.updated_at
        lines.append(f"reviewed   by {p.reviewed_by} at {when:%Y-%m-%d %H:%M}")
    if p.error:
        lines.append(f"error      {p.error}")
    if p.rollback_data is not None:
        lines.append("rollback   snapshot available — `b` restores the prior graph")
    lines.append("payload")
    lines += [f"  {line}" for line in json.dumps(p.payload, indent=2).splitlines()]
    if p.evidence:
        lines.append("evidence")
        lines += [f"  {line}" for line in json.dumps(p.evidence, indent=2).splitlines()]
    return lines


class ReviewTUI:
    def __init__(self, stdscr, db: Session):
        self.stdscr = stdscr
        self.db = db
        self.engine = ProposalEngine(db)
        self.user = default_reviewer()
        self.filter_idx = 0
        self.rows: list[Proposal] = []
        self.cursor = 0
        self.top = 0  # first visible list row
        self.detail_top = 0
        self.message: tuple[str, int] = ("", 0)
        self.reload()

    # -- state -----------------------------------------------------------------
    @property
    def status_filter(self) -> str | None:
        return FILTERS[self.filter_idx]

    @property
    def selected(self) -> Proposal | None:
        return self.rows[self.cursor] if self.rows else None

    def reload(self) -> None:
        self.rows = load_queue(self.db, self.status_filter)
        self.cursor = min(self.cursor, max(0, len(self.rows) - 1))
        self.detail_top = 0

    def move(self, delta: int) -> None:
        if self.rows:
            self.cursor = max(0, min(len(self.rows) - 1, self.cursor + delta))
            self.detail_top = 0

    # -- rendering ---------------------------------------------------------------
    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.stdscr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:  # bottom-right cell writes can error; ignore
                pass

    def draw(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        list_h = max(3, (h - 5) * 2 // 5)

        scope = self.status_filter or "all statuses"
        title = f" lorekeeper · proposal review · {scope}: {len(self.rows)} "
        self._put(0, 0, title.ljust(w - 1), curses.A_REVERSE | curses.A_BOLD)
        self._put(
            1,
            0,
            f"{'ID':<10}{'KIND':<24}{'CONF':<7}{'AGENT':<11}{'STATUS':<13}SUMMARY",
            curses.A_BOLD,
        )

        if self.cursor < self.top:
            self.top = self.cursor
        if self.cursor >= self.top + list_h:
            self.top = self.cursor - list_h + 1
        if not self.rows:
            self._put(3, 2, "queue is clean — nothing to review", curses.A_DIM)
        for i, p in enumerate(self.rows[self.top : self.top + list_h]):
            attr = curses.color_pair(_STATUS_COLOR.get(p.status, 0))
            if self.top + i == self.cursor:
                attr |= curses.A_REVERSE
            self._put(2 + i, 0, row_text(p).ljust(w - 1), attr)

        sep_y = 2 + list_h
        self._put(sep_y, 0, "─" * (w - 1), curses.A_DIM)
        if self.selected is not None:
            detail_h = h - sep_y - 2
            lines = _detail_lines(self.selected)
            self.detail_top = max(0, min(self.detail_top, len(lines) - detail_h))
            for i, line in enumerate(lines[self.detail_top : self.detail_top + detail_h]):
                self._put(sep_y + 1 + i, 1, line)

        text, attr = self.message if self.message[0] else (_HELP, curses.A_DIM)
        self._put(h - 1, 0, f" {text}", attr)
        self.stdscr.refresh()

    # -- actions -----------------------------------------------------------------
    def _confirm(self, prompt: str) -> bool:
        h, w = self.stdscr.getmaxyx()
        self._put(h - 1, 0, f" {prompt} [y/N] ".ljust(w - 1), curses.A_REVERSE)
        self.stdscr.refresh()
        return self.stdscr.getch() in (ord("y"), ord("Y"))

    def act(self, verb: str, allowed: set[str]) -> None:
        p = self.selected
        if p is None:
            return
        if p.status not in allowed:
            self.message = (
                f"cannot {verb} a '{p.status}' proposal",
                curses.color_pair(3),
            )
            return
        if not self._confirm(f"{verb} {str(p.id)[:8]} ({summarize(p)})?"):
            self.message = ("cancelled", curses.A_DIM)
            return
        try:
            p = getattr(self.engine, verb)(p.id, reviewed_by=self.user)
            self.message = (f"{verb}d {str(p.id)[:8]} -> {p.status}", curses.color_pair(2))
        except ProposalError as exc:
            self.message = (str(exc), curses.color_pair(3))
        self.reload()

    # -- main loop -----------------------------------------------------------------
    def run(self) -> None:
        curses.use_default_colors()
        palette = [curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_MAGENTA]
        for pair, color in enumerate(palette, start=1):
            curses.init_pair(pair, color, -1)
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        while True:
            self.draw()
            key = self.stdscr.getch()
            self.message = ("", 0)
            if key in (ord("q"), 27):  # q or ESC
                return
            elif key in (curses.KEY_UP, ord("k")):
                self.move(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.move(1)
            elif key == curses.KEY_PPAGE:
                self.move(-10)
            elif key == curses.KEY_NPAGE:
                self.move(10)
            elif key == ord("g"):
                self.move(-len(self.rows))
            elif key == ord("G"):
                self.move(len(self.rows))
            elif key == ord("u"):
                self.detail_top = max(0, self.detail_top - 5)
            elif key == ord("d"):
                self.detail_top += 5  # clamped against content in draw()
            elif key == ord("f"):
                self.filter_idx = (self.filter_idx + 1) % len(FILTERS)
                self.cursor = 0
                self.reload()
            elif key == ord("R"):
                self.reload()
                self.message = ("refreshed", curses.A_DIM)
            elif key in ACTIONS:
                self.act(*ACTIONS[key])


def run_tui() -> int:
    with SessionLocal() as db:
        curses.wrapper(lambda stdscr: ReviewTUI(stdscr, db).run())
    return 0
