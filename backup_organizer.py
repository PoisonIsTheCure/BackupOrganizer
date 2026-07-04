#!/usr/bin/env python3
"""BackupOrganizer — macOS backup manager and smart storage advisor.

Compresses configured "Sync" directories into a single timestamped zip,
archives files dropped into a Dropzone (hash-verified, then moved to Trash),
tracks state in a SHA-256 manifest, advises which local files are safely
deletable, and uploads the archive to Proton Drive.

Headless by design: run it from launchd for daily backups, or interactively
with --status / --advice. Requires only the Python standard library.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "BackupOrganizer"
MANIFEST_VERSION = 1
DEFAULT_CONFIG_PATH = Path("~/Backups/BackupOrganizer/config.json").expanduser()
ZIP_BIN = "/usr/bin/zip"
OSASCRIPT_BIN = "/usr/bin/osascript"
# Files newer than this many seconds are skipped in the dropzone so we never
# archive a copy that is still being written.
DROPZONE_SETTLE_SECONDS = 30
SUBPROCESS_TIMEOUT = 30 * 60  # uploads of large archives can be slow

QUOTA_ERROR_RE = re.compile(r"quota|storage.*(full|exceed)|insufficient|not enough space", re.I)
AUTH_ERROR_RE = re.compile(r"auth|login|session|unauthoriz|forbidden|credential|401|403", re.I)

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# Config & manifest
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "sync_dirs": ["~/Documents/Sync"],
    "dropzone": "~/Backups/ArchiveDropzone",
    "backup_dir": "~/Backups/BackupOrganizer",
    "manifest": "~/Backups/BackupOrganizer/manifest.json",
    "proton_cli": "/Users/alyz/developement/generalBin/proton-drive",
    "remote_folder": "/Backups",
    "remote_keep": 1,
    "min_free_gb": 2,
    "max_zip_gb": 0,
    "exclude": [".DS_Store", "*.tmp", "._*", ".localized"],
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    sync_dirs: list[Path]
    dropzone: Path
    backup_dir: Path
    manifest_path: Path
    proton_cli: str
    remote_folder: str
    remote_keep: int
    min_free_gb: float
    max_zip_gb: float
    exclude: list[str]

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise ConfigError(
                f"Config not found at {path}. Run with --init to create a default one."
            )
        try:
            raw = {**DEFAULT_CONFIG, **json.loads(path.read_text())}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

        cfg = cls(
            sync_dirs=[Path(p).expanduser() for p in raw["sync_dirs"]],
            dropzone=Path(raw["dropzone"]).expanduser(),
            backup_dir=Path(raw["backup_dir"]).expanduser(),
            manifest_path=Path(raw["manifest"]).expanduser(),
            proton_cli=str(raw["proton_cli"]),
            remote_folder=str(raw["remote_folder"]).rstrip("/") or "/",
            remote_keep=max(1, int(raw["remote_keep"])),
            min_free_gb=float(raw["min_free_gb"]),
            max_zip_gb=float(raw["max_zip_gb"]),
            exclude=list(raw["exclude"]),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.sync_dirs and not self.dropzone:
            raise ConfigError("Nothing to back up: no sync_dirs and no dropzone configured.")
        seen: dict[str, Path] = {}
        for d in self.sync_dirs:
            if d.name in seen:
                raise ConfigError(
                    f"Sync dirs {seen[d.name]} and {d} share the basename {d.name!r}; "
                    "their archive paths would collide. Rename one of them."
                )
            seen[d.name] = d
        for d in self.sync_dirs:
            if self.dropzone == d or self.dropzone in d.parents or d in self.dropzone.parents:
                raise ConfigError(f"Dropzone {self.dropzone} overlaps sync dir {d}.")

    def is_excluded(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.exclude)


@dataclass
class Manifest:
    path: Path
    zip_name: str = ""
    last_backup: str = ""
    last_upload: str = ""
    files: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text())
        return cls(
            path=path,
            zip_name=data.get("zip_name", ""),
            last_backup=data.get("last_backup", ""),
            last_upload=data.get("last_upload", ""),
            files=data.get("files", {}),
        )

    def save(self) -> None:
        payload = {
            "version": MANIFEST_VERSION,
            "zip_name": self.zip_name,
            "last_backup": self.last_backup,
            "last_upload": self.last_upload,
            "files": self.files,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def zip_path(self, cfg: Config) -> Path | None:
        return cfg.backup_dir / self.zip_name if self.zip_name else None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_zip_name() -> str:
    return datetime.datetime.now().strftime("backup_%Y%m%d_%H%M%S.zip")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def sha256_zip_entry(zf: zipfile.ZipFile, arcname: str) -> str:
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
    """Move a file or folder to the Finder Trash. Returns True on success."""
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


def escape_zip_pattern(arcname: str) -> str:
    """`zip -d` treats its arguments as glob patterns; escape the wildcards."""
    return re.sub(r"([*?\[\]])", r"\\\1", arcname)


class BackupError(Exception):
    """A fatal, already-logged error; the message is user-facing."""


# --------------------------------------------------------------------------- #
# Scanning & diffing
# --------------------------------------------------------------------------- #

@dataclass
class PendingFile:
    arcname: str
    origin: Path
    size: int
    mtime_ns: int
    sha256: str = ""  # filled lazily, only when needed
    source: str = "sync"


def walk_files(root: Path, cfg: Config) -> list[Path]:
    """All regular files under root, excludes applied, symlinks skipped."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not cfg.is_excluded(d))
        for name in sorted(filenames):
            if cfg.is_excluded(name):
                continue
            p = Path(dirpath) / name
            if p.is_symlink():
                log.debug("Skipping symlink %s", p)
                continue
            found.append(p)
    return found


@dataclass
class SyncDiff:
    added: list[PendingFile] = field(default_factory=list)
    changed: list[PendingFile] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # arcnames
    touched: list[PendingFile] = field(default_factory=list)  # stat changed, hash identical


def diff_sync_dirs(cfg: Config, manifest: Manifest) -> SyncDiff:
    """Compare local sync dirs to the manifest.

    I/O-efficient: files whose (size, mtime_ns) match the manifest are trusted
    without reading a byte; only new/stat-changed files get hashed.
    """
    diff = SyncDiff()
    seen: set[str] = set()

    for sync_dir in cfg.sync_dirs:
        if not sync_dir.is_dir():
            log.warning("Sync dir missing, skipping: %s", sync_dir)
            continue
        prefix = f"Sync/{sync_dir.name}/"
        for path in walk_files(sync_dir, cfg):
            arcname = prefix + path.relative_to(sync_dir).as_posix()
            seen.add(arcname)
            st = path.stat()
            entry = manifest.files.get(arcname)
            if entry and entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns:
                continue  # fast path: unchanged
            pf = PendingFile(arcname, path, st.st_size, st.st_mtime_ns)
            pf.sha256 = sha256_file(path)
            if entry is None:
                diff.added.append(pf)
            elif entry["sha256"] == pf.sha256:
                diff.touched.append(pf)  # metadata refresh only, no zip write
            else:
                diff.changed.append(pf)

    active_prefixes = tuple(f"Sync/{d.name}/" for d in cfg.sync_dirs if d.is_dir())
    diff.deleted = [
        arc
        for arc, entry in manifest.files.items()
        if entry.get("source") == "sync"
        and arc.startswith(active_prefixes)
        and arc not in seen
    ]
    return diff


def scan_dropzone(cfg: Config, manifest: Manifest) -> tuple[list[PendingFile], dict[Path, list[str]]]:
    """Collect dropzone files ready for archiving.

    Returns the pending files plus a map of top-level dropzone entries to the
    arcnames they contain, so an entry is only trashed once every file in it
    has been verified inside the zip.
    """
    pending: list[PendingFile] = []
    groups: dict[Path, list[str]] = {}
    if not cfg.dropzone.is_dir():
        return pending, groups

    cutoff = datetime.datetime.now().timestamp() - DROPZONE_SETTLE_SECONDS
    existing_names = set(manifest.files)

    for top in sorted(cfg.dropzone.iterdir()):
        if top.name.startswith(".") or cfg.is_excluded(top.name):
            continue
        files = walk_files(top, cfg) if top.is_dir() else ([top] if top.is_file() else [])
        if not files:
            continue
        if any(f.stat().st_mtime > cutoff for f in files):
            log.info("Dropzone entry still settling, skipping this run: %s", top.name)
            continue
        group: list[str] = []
        for path in files:
            st = path.stat()
            digest = sha256_file(path)
            arcname = "Archive/" + path.relative_to(cfg.dropzone).as_posix()
            prior = manifest.files.get(arcname)
            if prior and prior["sha256"] != digest:
                # Same name, different content: keep both by timestamping the new one.
                stem, dot, ext = arcname.rpartition(".")
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                arcname = f"{stem}_{stamp}.{ext}" if dot else f"{arcname}_{stamp}"
                while arcname in existing_names:
                    arcname += "_1"
            existing_names.add(arcname)
            pf = PendingFile(arcname, path, st.st_size, st.st_mtime_ns, digest, source="dropzone")
            pending.append(pf)
            group.append(arcname)
        groups[top] = group
    return pending, groups


# --------------------------------------------------------------------------- #
# Zip operations
# --------------------------------------------------------------------------- #

def check_free_space(cfg: Config, needed_bytes: int) -> None:
    margin = int(cfg.min_free_gb * 1024**3)
    free = shutil.disk_usage(cfg.backup_dir).free
    if free < needed_bytes + margin:
        raise BackupError(
            f"Low disk space: need ~{human_size(needed_bytes + margin)} free "
            f"(including {cfg.min_free_gb} GB margin), have {human_size(free)}."
        )


def build_zip(zip_path: Path, files: list[PendingFile]) -> None:
    """Create a fresh archive from scratch (first run / recovery)."""
    tmp = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for pf in files:
            zf.write(pf.origin, pf.arcname)
    os.replace(tmp, zip_path)


def update_zip(zip_path: Path, to_write: list[PendingFile], to_delete: list[str]) -> None:
    """Update an existing archive in place, touching only changed entries.

    Added/changed files are staged as symlinks in a temp tree that mirrors the
    archive layout, then handed to the system `zip` tool, which recompresses
    only those entries (it rewrites the archive container, but never re-reads
    or recompresses unchanged file data). Deletions use `zip -d`.
    """
    if to_delete:
        args = [ZIP_BIN, "-q", "-d", str(zip_path)]
        args += [escape_zip_pattern(a) for a in to_delete]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
        if proc.returncode not in (0, 12):  # 12 = nothing matched
            raise BackupError(f"zip -d failed ({proc.returncode}): {proc.stderr.strip()}")

    if to_write:
        with tempfile.TemporaryDirectory(dir=zip_path.parent) as staging:
            for pf in to_write:
                link = Path(staging) / pf.arcname
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(pf.origin, link)
            # Default add mode replaces existing entries unconditionally
            # (unlike -u, which trusts mtimes). zip follows symlinks by default.
            args = [ZIP_BIN, "-q", "-X", str(zip_path)] + [pf.arcname for pf in to_write]
            proc = subprocess.run(
                args, cwd=staging, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
            )
            if proc.returncode != 0:
                raise BackupError(f"zip update failed ({proc.returncode}): {proc.stderr.strip()}")


def verify_zip(zip_path: Path, dropzone_files: list[PendingFile]) -> list[PendingFile]:
    """CRC-check the archive and hash-verify every dropzone entry.

    Returns the dropzone files whose in-zip content matches their local hash.
    """
    verified: list[PendingFile] = []
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise BackupError(f"Archive integrity check failed at entry: {bad}")
        for pf in dropzone_files:
            if sha256_zip_entry(zf, pf.arcname) == pf.sha256:
                verified.append(pf)
            else:
                log.error("Hash mismatch inside zip for %s — local file kept.", pf.arcname)
    return verified


# --------------------------------------------------------------------------- #
# Proton Drive upload
# --------------------------------------------------------------------------- #

class UploadError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "quota" | "auth" | "error"


def classify_upload_error(output: str) -> str:
    if QUOTA_ERROR_RE.search(output):
        return "quota"
    if AUTH_ERROR_RE.search(output):
        return "auth"
    return "error"


def proton(cfg: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cfg.proton_cli, *args], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
    )


def upload_backup(cfg: Config, zip_path: Path, manifest: Manifest) -> None:
    if cfg.max_zip_gb > 0:
        size = zip_path.stat().st_size
        if size > cfg.max_zip_gb * 1024**3:
            raise UploadError(
                "quota",
                f"Archive is {human_size(size)}, over the configured "
                f"max_zip_gb limit of {cfg.max_zip_gb} GB — upload skipped.",
            )

    # Ensure the remote folder exists; "already exists" errors are fine.
    parent = str(Path(cfg.remote_folder).parent).replace("\\", "/") or "/"
    name = Path(cfg.remote_folder).name
    if name:
        proc = proton(cfg, "filesystem", "create-folder", parent, name)
        if proc.returncode != 0 and "exist" not in (proc.stderr + proc.stdout).lower():
            log.warning("create-folder %s: %s", cfg.remote_folder, proc.stderr.strip())

    proc = proton(
        cfg,
        "filesystem", "upload", "-c", "replace", "-t",
        str(zip_path), str(manifest.path), cfg.remote_folder,
    )
    if proc.returncode != 0:
        output = proc.stderr + proc.stdout
        raise UploadError(classify_upload_error(output), output.strip()[:500] or "unknown error")

    prune_remote(cfg, keep_name=zip_path.name)


def prune_remote(cfg: Config, keep_name: str) -> None:
    """Trash remote backup zips beyond remote_keep. Best effort, never fatal."""
    try:
        proc = proton(cfg, "--json", "filesystem", "list", cfg.remote_folder)
        if proc.returncode != 0:
            log.warning("Remote list failed, skipping prune: %s", proc.stderr.strip())
            return
        names: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                name = item.get("name") or item.get("Name") or ""
            except json.JSONDecodeError:
                name = line
            if re.fullmatch(r"backup_\d{8}_\d{6}\.zip", name):
                names.append(name)
        names = sorted(n for n in set(names) if n != keep_name)
        excess = names[: max(0, len(names) - (cfg.remote_keep - 1))]
        for name in excess:
            remote_path = f"{cfg.remote_folder}/{name}"
            proc = proton(cfg, "filesystem", "trash", remote_path)
            if proc.returncode == 0:
                log.info("Trashed old remote backup %s", remote_path)
            else:
                log.warning("Could not trash %s: %s", remote_path, proc.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Remote prune skipped: %s", exc)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_backup(cfg: Config, do_upload: bool, dry_run: bool, notify_enabled: bool) -> int:
    manifest = Manifest.load(cfg.manifest_path)
    zip_path = manifest.zip_path(cfg)

    if zip_path and not zip_path.is_file():
        lost = [a for a, e in manifest.files.items() if e.get("source") == "dropzone"]
        log.error(
            "Archive %s is missing from disk; rebuilding. %d archived dropzone "
            "entr%s no longer backed up: %s",
            zip_path, len(lost), "y is" if len(lost) == 1 else "ies are",
            ", ".join(lost) or "none",
        )
        if lost:
            notify(
                APP_NAME,
                f"Backup zip was missing — rebuilt, but {len(lost)} archived "
                "file(s) were lost with it. See the log.",
                notify_enabled,
            )
        manifest = Manifest(path=cfg.manifest_path)
        zip_path = None

    diff = diff_sync_dirs(cfg, manifest)
    dropzone_files, dropzone_groups = scan_dropzone(cfg, manifest)

    to_write = diff.added + diff.changed + dropzone_files
    n_changes = len(to_write) + len(diff.deleted)
    log.info(
        "Diff: %d added, %d changed, %d deleted, %d touched, %d dropzone file(s).",
        len(diff.added), len(diff.changed), len(diff.deleted),
        len(diff.touched), len(dropzone_files),
    )

    if dry_run:
        for label, items in (
            ("ADD", [pf.arcname for pf in diff.added]),
            ("UPDATE", [pf.arcname for pf in diff.changed]),
            ("DELETE", diff.deleted),
            ("ARCHIVE", [pf.arcname for pf in dropzone_files]),
        ):
            for arc in items:
                print(f"{label:8} {arc}")
        print(f"\n{n_changes} change(s) would be written. Nothing was modified.")
        return 0

    if n_changes == 0 and not diff.touched:
        log.info("Everything up to date; no zip write needed.")
        manifest.last_backup = now_iso()
        if zip_path:
            manifest.save()
        if do_upload and manifest.zip_name and not manifest.last_upload:
            _try_upload(cfg, manifest, notify_enabled)
        notify(APP_NAME, "Backup OK — no changes.", notify_enabled)
        return 0

    # ----- write the archive -------------------------------------------- #
    if n_changes:
        delta = sum(pf.size for pf in to_write)
        current = zip_path.stat().st_size if zip_path else 0
        # `zip` rewrites the container to a temp copy next to the archive, so
        # the worst case needs room for the old archive plus the new data.
        check_free_space(cfg, current + delta)

        if zip_path is None:
            manifest.zip_name = new_zip_name()
            zip_path = manifest.zip_path(cfg)
            assert zip_path is not None
            build_zip(zip_path, to_write)
        else:
            update_zip(zip_path, to_write, diff.deleted)

        verified = verify_zip(zip_path, dropzone_files)
        verified_set = {pf.arcname for pf in verified}

        # Rotate the filename so it always names the latest successful backup.
        fresh = cfg.backup_dir / new_zip_name()
        if fresh != zip_path:
            os.rename(zip_path, fresh)
            zip_path = fresh
            manifest.zip_name = fresh.name
    else:
        verified_set = set()

    # ----- update manifest ------------------------------------------------ #
    for arc in diff.deleted:
        manifest.files.pop(arc, None)
    for pf in diff.added + diff.changed + diff.touched:
        manifest.files[pf.arcname] = {
            "size": pf.size, "mtime_ns": pf.mtime_ns,
            "sha256": pf.sha256, "source": "sync", "origin": str(pf.origin),
        }
    for pf in dropzone_files:
        if pf.arcname in verified_set:
            manifest.files[pf.arcname] = {
                "size": pf.size, "mtime_ns": pf.mtime_ns,
                "sha256": pf.sha256, "source": "dropzone", "origin": str(pf.origin),
            }
    manifest.last_backup = now_iso()
    manifest.save()

    # ----- trash verified dropzone originals ------------------------------ #
    trashed = 0
    unverified_groups = 0
    for top, arcnames in dropzone_groups.items():
        if all(a in verified_set for a in arcnames):
            if move_to_trash(top):
                trashed += 1
            log.info("Archived and trashed dropzone entry: %s", top.name)
        else:
            unverified_groups += 1
            log.error("Dropzone entry NOT trashed (verification failed): %s", top.name)

    # ----- upload ---------------------------------------------------------- #
    upload_note = ""
    if do_upload:
        upload_note = _try_upload(cfg, manifest, notify_enabled)

    summary = (
        f"{len(diff.added)} added, {len(diff.changed)} updated, "
        f"{len(diff.deleted)} removed"
    )
    if dropzone_files:
        summary += f", {trashed} dropzone item(s) archived"
    if unverified_groups:
        notify(APP_NAME, f"Backup finished with ERRORS — {unverified_groups} "
                         "dropzone item(s) failed verification. See the log.", notify_enabled)
        return 1
    notify(APP_NAME, f"Backup OK — {summary}.{upload_note}", notify_enabled)
    return 0


def _try_upload(cfg: Config, manifest: Manifest, notify_enabled: bool) -> str:
    """Upload and report. Returns a suffix for the success notification."""
    zip_path = manifest.zip_path(cfg)
    if not zip_path or not zip_path.is_file():
        return ""
    try:
        log.info("Uploading %s to Proton Drive %s ...", zip_path.name, cfg.remote_folder)
        upload_backup(cfg, zip_path, manifest)
        manifest.last_upload = now_iso()
        manifest.save()
        log.info("Upload complete.")
        return " Uploaded to Proton Drive."
    except UploadError as exc:
        log.error("Upload failed (%s): %s", exc.kind, exc)
        messages = {
            "quota": "Proton Drive storage is FULL — upload failed. Free up space in your plan.",
            "auth": "Proton Drive login expired — run `proton-drive auth login`.",
            "error": "Proton Drive upload failed (network/CLI error). Backup is safe locally.",
        }
        notify(APP_NAME, messages[exc.kind], notify_enabled)
        return " Upload FAILED (will retry next run)."
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Upload failed: %s", exc)
        notify(APP_NAME, "Proton Drive upload failed — see the log.", notify_enabled)
        return " Upload FAILED (will retry next run)."


def cmd_status(cfg: Config) -> int:
    manifest = Manifest.load(cfg.manifest_path)
    zip_path = manifest.zip_path(cfg)
    total = len(manifest.files)
    total_bytes = sum(e["size"] for e in manifest.files.values())
    sync_n = sum(1 for e in manifest.files.values() if e.get("source") == "sync")
    arch_n = total - sync_n
    pending = 0
    if cfg.dropzone.is_dir():
        pending = sum(
            1 for p in cfg.dropzone.iterdir()
            if not p.name.startswith(".") and not cfg.is_excluded(p.name)
        )

    lines = [
        f"{APP_NAME} status",
        "-" * 34,
        f"Files backed up:   {total}  ({sync_n} sync, {arch_n} archived)",
        f"Total size:        {human_size(total_bytes)}",
        f"Archive:           {manifest.zip_name or '(none yet)'}",
    ]
    if zip_path and zip_path.is_file():
        lines.append(f"Archive size:      {human_size(zip_path.stat().st_size)}")
    elif manifest.zip_name:
        lines.append("Archive size:      MISSING FROM DISK!")
    lines += [
        f"Last backup:       {manifest.last_backup or 'never'}",
        f"Last upload:       {manifest.last_upload or 'never'}",
        f"Dropzone pending:  {pending} item(s)",
    ]
    print("\n".join(lines))
    return 0


def cmd_advice(cfg: Config, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"Not a directory: {target}", file=sys.stderr)
        return 2
    manifest = Manifest.load(cfg.manifest_path)
    zip_path = manifest.zip_path(cfg)
    if not manifest.files or not zip_path or not zip_path.is_file():
        print("No backup archive on disk yet — nothing is safe to delete.")
        return 1

    by_hash = {e["sha256"]: arc for arc, e in manifest.files.items()}
    by_origin = {e["origin"]: (arc, e) for arc, e in manifest.files.items()}

    safe: list[tuple[Path, int, str]] = []
    unsafe: list[Path] = []
    for path in walk_files(target, cfg):
        st = path.stat()
        known = by_origin.get(str(path))
        if known and known[1]["size"] == st.st_size and known[1]["mtime_ns"] == st.st_mtime_ns:
            safe.append((path, st.st_size, known[0]))  # fast path, no read
            continue
        arc = by_hash.get(sha256_file(path))
        if arc is not None:
            safe.append((path, st.st_size, arc))
        else:
            unsafe.append(path)

    print(f"Safe-to-delete report for: {target}")
    print(f"(verified against {manifest.zip_name}, last backup {manifest.last_backup})")
    print()
    if safe:
        print(f"SAFE TO DELETE — {len(safe)} file(s), byte-identical copies exist in the backup:")
        for path, size, arc in safe:
            print(f"  {path}  [{human_size(size)}]  -> {arc}")
        print(f"\n  Reclaimable: {human_size(sum(s for _, s, _ in safe))}")
    else:
        print("SAFE TO DELETE: none.")
    print()
    if unsafe:
        print(f"NOT BACKED UP — {len(unsafe)} file(s), do NOT delete:")
        for path in unsafe:
            print(f"  {path}")
    else:
        print("NOT BACKED UP: none — everything here is covered.")
    print("\nThis report is advisory only; nothing was deleted.")
    return 0


def cmd_init(config_path: Path) -> int:
    if config_path.exists():
        print(f"Config already exists: {config_path}")
        return 1
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    Path(DEFAULT_CONFIG["dropzone"]).expanduser().mkdir(parents=True, exist_ok=True)
    Path(DEFAULT_CONFIG["backup_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
    print(f"Wrote default config: {config_path}")
    print("Edit sync_dirs before the first run.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def setup_logging(backup_dir: Path, verbose: bool) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backup-organizer",
        description="macOS backup manager and smart storage advisor.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--status", action="store_true", help="print backup summary and exit")
    parser.add_argument("--advice", metavar="DIR", type=Path,
                        help="report files in DIR that are safely backed up")
    parser.add_argument("--init", action="store_true", help="write a default config and exit")
    parser.add_argument("--no-upload", action="store_true", help="skip the Proton Drive upload")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    parser.add_argument("--no-notify", action="store_true", help="suppress macOS notifications")
    parser.add_argument("--verbose", action="store_true", help="chatty console output")
    args = parser.parse_args(argv)

    if args.init:
        return cmd_init(args.config)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.backup_dir, args.verbose)
    notify_enabled = not args.no_notify

    if args.status:
        return cmd_status(cfg)
    if args.advice:
        return cmd_advice(cfg, args.advice)

    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(cfg.backup_dir / ".lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another instance is already running; exiting.")
        return 0

    try:
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


if __name__ == "__main__":
    sys.exit(main())
