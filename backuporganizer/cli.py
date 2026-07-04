"""Argument parsing, the single-instance lock, and command dispatch."""

from __future__ import annotations

import argparse
import fcntl
import sys
from pathlib import Path

from .commands import (cmd_advice, cmd_backup, cmd_dedupe, cmd_init,
                       cmd_restore, cmd_status)
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError
from .util import APP_NAME, BackupError, log, notify, setup_logging
from .viewer import cmd_browse, cmd_tree


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Read-only commands run without the lock; anything
    that can write chunks (backup, dedupe) runs under it."""
    parser = argparse.ArgumentParser(
        prog="backup-organizer",
        description="macOS backup manager and smart storage advisor.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--status", action="store_true", help="print backup summary and exit")
    parser.add_argument("--advice", metavar="DIR", type=Path,
                        help="report files in DIR that are safely backed up")
    parser.add_argument("--tree", metavar="PREFIX", nargs="?", const="",
                        help="print the backed-up file tree (optionally under PREFIX)")
    parser.add_argument("--browse", metavar="OUT", nargs="?", const="", type=str,
                        help="generate an interactive HTML browser of the backup and open it")
    parser.add_argument("--restore", metavar="NAME",
                        help="restore file(s)/folder(s) matching a name, substring, "
                             "glob, or folder path ending in /")
    parser.add_argument("--restore-all", action="store_true",
                        help="download and restore the entire backup (see --dest)")
    parser.add_argument("--dedupe", metavar="MIN_MB", nargs="?", const=1.0, type=float,
                        help="interactively remove duplicate copies of the same file "
                             "(default: only files of at least 1 MB)")
    parser.add_argument("--dest", metavar="DIR", type=Path,
                        default=Path("~/Downloads/BackupOrganizer-Restore"),
                        help="destination for --restore (default: %(default)s)")
    parser.add_argument("--init", action="store_true", help="write a default config and exit")
    parser.add_argument("--no-upload", action="store_true", help="skip the Proton Drive upload")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    parser.add_argument("--no-notify", action="store_true", help="suppress macOS notifications")
    parser.add_argument("--verbose", action="store_true", help="chatty console output")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, load config, dispatch. Returns the exit code
    (0 success, 1 failure, 2 configuration error)."""
    args = build_parser().parse_args(argv)

    if args.init:
        return cmd_init(args.config)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.backup_dir, args.verbose)
    notify_enabled = not args.no_notify

    # Read-only commands need no lock.
    if args.status:
        return cmd_status(cfg)
    if args.tree is not None:
        return cmd_tree(cfg, args.tree)
    if args.browse is not None:
        return cmd_browse(cfg, Path(args.browse) if args.browse else None)
    if args.advice:
        return cmd_advice(cfg, args.advice)
    if args.restore_all:
        return cmd_restore(cfg, "*", args.dest)
    if args.restore:
        return cmd_restore(cfg, args.restore, args.dest)

    # backup / dedupe write chunks: take the single-instance lock.
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(cfg.backup_dir / ".lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another instance is already running; exiting.")
        return 0

    try:
        if args.dedupe is not None:
            return cmd_dedupe(cfg, args.dedupe, notify_enabled)
        return cmd_backup(cfg, do_upload=not args.no_upload,
                          dry_run=args.dry_run, notify_enabled=notify_enabled)
    except BackupError as exc:
        log.error("%s", exc)
        notify(APP_NAME, f"Backup FAILED: {exc}", notify_enabled)
        return 1
    except Exception:
        log.exception("Unexpected failure")
        notify(APP_NAME, "Backup FAILED with an unexpected error — see the log.", notify_enabled)
        return 1
    finally:
        lock_file.close()
