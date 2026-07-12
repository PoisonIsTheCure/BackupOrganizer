"""Argument parsing, the single-instance lock, and command dispatch."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

from .commands import (cmd_add_sync, cmd_advice, cmd_archive, cmd_backup,
                       cmd_dedupe, cmd_delete_remote, cmd_init, cmd_list,
                       cmd_log_tail, cmd_orphans, cmd_recalculate_manifest,
                       cmd_remove_sync, cmd_restore, cmd_retire_sync_twin, cmd_status)
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError
from .util import APP_NAME, BackupError, install_pause_handler, log, notify, setup_logging
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
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON (status/list/orphans/archive/add-sync)")
    parser.add_argument("--status", action="store_true", help="print backup summary and exit")
    parser.add_argument("--list", metavar="KIND", nargs="?", const="all",
                        choices=["sync", "archive", "all"],
                        help="list backed-up files (sync, archive, or all)")
    parser.add_argument("--orphans", action="store_true",
                        help="list synced files deleted locally but still in the cloud")
    parser.add_argument("--log-tail", metavar="N", nargs="?", const=200, type=int,
                        help="print the last N lines of backup_organizer.log (default: 200)")
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

    sub = parser.add_subparsers(dest="command", metavar="command",
                                title="commands (optional; no command = run)")
    sub.add_parser("run", help="full cycle: scan for changes, build chunks, "
                               "upload to Proton Drive, free local space")
    archive_p = sub.add_parser(
        "archive", help="move files/folders into the dropzone and run the cycle")
    archive_p.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    archive_p.add_argument("--no-run", action="store_true",
                           help="only stage into the dropzone; archive on the next run")
    addsync_p = sub.add_parser(
        "add-sync", help="add folder(s) to sync_dirs and run the cycle")
    addsync_p.add_argument("dirs", nargs="+", type=Path, metavar="DIR")
    addsync_p.add_argument("--no-run", action="store_true",
                           help="only update the config; back up on the next run")
    removesync_p = sub.add_parser(
        "remove-sync",
        help="stop syncing folder(s); their cloud copies become orphans, kept until deleted-remote")
    removesync_p.add_argument("dirs", nargs="+", type=Path, metavar="DIR")
    del_p = sub.add_parser(
        "delete-remote", help="permanently delete the cloud copy of an orphaned synced file")
    del_p.add_argument("arcnames", nargs="+", metavar="ARCNAME")
    retire_p = sub.add_parser(
        "retire-sync-twin",
        help="retire the synced copy of an already-archived file (local + cloud)")
    retire_p.add_argument("arcnames", nargs="+", metavar="ARCHIVE_ARCNAME")
    sub.add_parser(
        "recalculate-manifest",
        help="reconcile the local manifest against what's actually on Proton Drive")
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
    install_pause_handler()
    notify_enabled = not args.no_notify

    # Read-only commands need no lock.
    if args.status:
        return cmd_status(cfg, json_out=args.json)
    if args.list is not None:
        return cmd_list(cfg, args.list, json_out=args.json)
    if args.orphans:
        return cmd_orphans(cfg, json_out=args.json)
    if args.log_tail is not None:
        return cmd_log_tail(cfg, args.log_tail, json_out=args.json)
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
        if args.command == "archive":
            return cmd_archive(cfg, args.paths, run=not args.no_run,
                               do_upload=not args.no_upload,
                               notify_enabled=notify_enabled, json_out=args.json)
        if args.command == "add-sync":
            try:
                return cmd_add_sync(args.config, args.dirs, run=not args.no_run,
                                    do_upload=not args.no_upload,
                                    notify_enabled=notify_enabled, json_out=args.json)
            except ConfigError as exc:
                print(f"Config error: {exc}", file=sys.stderr)
                return 2
        if args.command == "remove-sync":
            try:
                return cmd_remove_sync(args.config, args.dirs, json_out=args.json)
            except ConfigError as exc:
                print(f"Config error: {exc}", file=sys.stderr)
                return 2
        if args.command == "delete-remote":
            return cmd_delete_remote(cfg, args.arcnames, json_out=args.json)
        if args.command == "retire-sync-twin":
            return cmd_retire_sync_twin(cfg, args.arcnames, json_out=args.json)
        if args.command == "recalculate-manifest":
            return cmd_recalculate_manifest(cfg, json_out=args.json)
        # bare invocation or explicit `run`: the full cycle
        return cmd_backup(cfg, do_upload=not args.no_upload,
                          dry_run=args.dry_run, notify_enabled=notify_enabled,
                          json_out=args.json)
    except BackupError as exc:
        log.error("%s", exc)
        notify(APP_NAME, f"Backup FAILED: {exc}", notify_enabled)
        return 1
    except BrokenPipeError:
        # Whoever was reading our stdout (the GUI, a shell pipe) is gone.
        # cmd_backup's own JSON progress output already tolerates this;
        # this is the safety net for everything else that still calls a
        # bare print() (dry-run listings, human-readable --status, ...).
        # Silence Python's noisy "Exception ignored" on shutdown by
        # redirecting stdout to /dev/null before we exit.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 1
    except Exception:
        log.exception("Unexpected failure")
        notify(APP_NAME, "Backup FAILED with an unexpected error — see the log.", notify_enabled)
        return 1
    finally:
        lock_file.close()
