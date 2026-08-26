"""Labeled graph backups via pg_dump, with retention pruning.

    python -m app.backup run [--keep N] [--label TEXT]
    python -m app.backup list
    python -m app.backup restore FILE     (prints the pg_restore command)

Backups are plain SQL dumps written into BACKUP_DIR (default ./backups).
Retention keeps the newest N and never deletes anything outside the backup dir.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings


def backup_dir() -> Path:
    path = Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def dump_command() -> list[str]:
    # pg_dump reads the password from the environment; it never lands in argv.
    os.environ.setdefault("PGPASSWORD", settings.postgres_password)
    return [
        "pg_dump",
        "--host",
        settings.postgres_host,
        "--port",
        str(settings.postgres_port),
        "--username",
        settings.postgres_user,
        "--no-owner",
        "--format",
        "plain",
        settings.postgres_db,
    ]


def snapshot_name(label: str | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    return f"lorekeeper-{stamp}{suffix}.sql"


def plan_prune(names: list[str], keep: int) -> list[str]:
    """The oldest dump names beyond the newest `keep` (sorted by name = time)."""
    ordered = sorted(n for n in names if n.endswith(".sql"))
    return ordered[:-keep] if keep > 0 else ordered


def run_backup(label: str | None, keep: int) -> int:
    directory = backup_dir()
    target = directory / snapshot_name(label)
    cmd = dump_command()
    print(f"dumping {settings.postgres_db} -> {target}")
    with open(target, "wb") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        print(f"pg_dump failed: {result.stderr.decode(errors='replace')[:300]}", file=sys.stderr)
        return 1

    dumps = [p.name for p in directory.glob("*.sql")]
    doomed = plan_prune(dumps, keep)
    for name in doomed:
        (directory / name).unlink()
    kept = len(dumps) - len(doomed)
    print(f"wrote {target.name}; {kept} dump(s) kept, {len(doomed)} pruned")
    return 0


def list_backups() -> int:
    dumps = sorted(p.name for p in backup_dir().glob("*.sql"))
    if not dumps:
        print("no backups yet")
        return 0
    for name in dumps:
        size_kb = (backup_dir() / name).stat().st_size // 1024
        print(f"{name}  {size_kb} KB")
    return 0


def restore_hint(filename: str) -> int:
    target = backup_dir() / filename
    if not target.exists():
        print(f"no such backup: {target}", file=sys.stderr)
        return 1
    print("Review, then run:")
    print(
        f"PGPASSWORD=*** psql --host {settings.postgres_host} "
        f"--username {settings.postgres_user} --dbname {settings.postgres_db} < {target}"
    )
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Take a labeled dump and prune old ones.")
    run_p.add_argument("--label", default=None, help="Short label baked into the filename.")
    run_p.add_argument(
        "--keep", type=int, default=settings.backup_keep, help="Newest N dumps to retain."
    )

    sub.add_parser("list", help="List existing backups.")

    res_p = sub.add_parser("restore", help="Print the restore command for one dump.")
    res_p.add_argument("file")

    args = parser.parse_args()
    if args.cmd == "run":
        if args.keep < 0:
            print("--keep must be >= 0", file=sys.stderr)
            return 2
        return run_backup(args.label, args.keep)
    if args.cmd == "list":
        return list_backups()
    return restore_hint(args.file)


if __name__ == "__main__":
    raise SystemExit(_main())
