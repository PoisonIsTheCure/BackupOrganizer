"""Shared helpers: logging, hashing, notifications, and the Finder Trash."""

from __future__ import annotations

import datetime
import hashlib
import logging
import logging.handlers
import subprocess
import zipfile
from pathlib import Path

APP_NAME = "BackupOrganizer"
OSASCRIPT_BIN = "/usr/bin/osascript"

log = logging.getLogger(APP_NAME)


class BackupError(Exception):
    """A fatal, already-logged error; the message is user-facing."""


def setup_logging(backup_dir: Path, verbose: bool) -> None:
    """Log INFO+ to a rotating file in backup_dir and WARNING+ (or DEBUG+
    with verbose) to the console."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            backup_dir / "backup_organizer.log", maxBytes=1024 * 1024, backupCount=3
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        log.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    log.addHandler(sh)


def now_iso() -> str:
    """Current local time as an ISO-8601 string with timezone."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def human_size(n: float) -> str:
    """Format a byte count for humans: 1234 -> '1.2 KB'."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path) -> str:
    """SHA-256 of a file on disk, hashed in C via hashlib.file_digest."""
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def sha256_zip_entry(zf: zipfile.ZipFile, arcname: str) -> str:
    """SHA-256 of one entry inside an open zip, streamed in 1 MiB pieces."""
    digest = hashlib.sha256()
    with zf.open(arcname) as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def notify(title: str, message: str, enabled: bool = True) -> None:
    """Post a macOS notification via osascript. Failures are logged, never fatal."""
    if not enabled:
        return
    script = 'display notification "{}" with title "{}"'.format(
        message.replace("\\", "\\\\").replace('"', '\\"'),
        title.replace("\\", "\\\\").replace('"', '\\"'),
    )
    try:
        subprocess.run([OSASCRIPT_BIN, "-e", script], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Notification failed: %s", exc)


def move_to_trash(path: Path) -> bool:
    """Move a file or folder to the Finder Trash (recoverable, unlike unlink).
    Returns True on success; failures are logged and returned as False."""
    script = 'tell application "Finder" to delete POSIX file "{}"'.format(
        str(path).replace("\\", "\\\\").replace('"', '\\"')
    )
    try:
        proc = subprocess.run(
            [OSASCRIPT_BIN, "-e", script], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Trash of %s failed: %s", path, exc)
        return False
    if proc.returncode != 0:
        log.error("Trash of %s failed: %s", path, proc.stderr.strip())
        return False
    return True
