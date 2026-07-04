#!/usr/bin/env python3
"""BackupOrganizer — macOS backup manager and smart storage advisor.

Compresses configured "Sync" directories and an "Archive Dropzone" into
size-capped zip chunks, uploads them to Proton Drive, and then removes the
local chunks to free disk space. State lives in a SHA-256 manifest; changed
files only ever rebuild and re-upload their own chunk, and restoring a file
only downloads the one chunk that contains it.

Headless by design: run it from launchd for daily backups, or interactively
with --status / --advice / --restore. Requires only the standard library.
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
MANIFEST_VERSION = 2
DEFAULT_CONFIG_PATH = Path("~/Backups/BackupOrganizer/config.json").expanduser()
OSASCRIPT_BIN = "/usr/bin/osascript"
# Files newer than this many seconds are skipped in the dropzone so we never
# archive a copy that is still being written.
DROPZONE_SETTLE_SECONDS = 30
SUBPROCESS_TIMEOUT = 15 * 60
TRANSFER_TIMEOUT = 6 * 60 * 60  # multi-GB chunks on a slow uplink take a while

# Already-compressed formats are stored, not deflated: recompressing a video
# burns minutes of CPU for well under 1% size gain.
STORED_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg",
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".dmg",
}

QUOTA_ERROR_RE = re.compile(r"quota|storage.*(full|exceed)|insufficient|not enough space", re.I)
AUTH_ERROR_RE = re.compile(r"auth|login|session|unauthoriz|forbidden|credential|401|403", re.I)

# The Proton Drive CLI only accepts paths inside one of its root namespaces
# (`filesystem list /` shows them); a bare folder path like /Backups is
# rejected with: Path "/Backups" not supported.
PROTON_NAMESPACES = {
    "my-files", "devices", "shared-by-me", "shared-with-me", "albums",
    "photos", "photos-shared-by-me", "photos-shared-with-me",
}

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
    "remote_folder": "/my-files/Backups/MacBookAir",
    "chunk_mb": 500,
    "keep_local_chunks": False,
    "min_free_gb": 2,
    "exclude": [".DS_Store", "*.tmp", "._*", ".localized"],
}


class ConfigError(Exception):
    pass


def normalize_remote_folder(raw: str) -> str:
    """Anchor the remote folder inside a Proton Drive namespace.

    "/Backups/Mac" and "Backups/Mac" both become "/my-files/Backups/Mac";
    a path that already names a namespace is kept as-is.
    """
    parts = [p for p in raw.strip("/").split("/") if p]
    if not parts:
        return "/my-files"
    if parts[0] not in PROTON_NAMESPACES:
        parts.insert(0, "my-files")
    return "/" + "/".join(parts)


@dataclass
class Config:
    sync_dirs: list[Path]
    dropzone: Path
    backup_dir: Path
    manifest_path: Path
    proton_cli: str
    remote_folder: str
    chunk_mb: float
    keep_local_chunks: bool
    min_free_gb: float
    exclude: list[str]

    @property
    def chunk_cap(self) -> int:
        return max(1, int(self.chunk_mb * 1024**2))

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
            remote_folder=normalize_remote_folder(str(raw["remote_folder"])),
            chunk_mb=float(raw["chunk_mb"]),
            keep_local_chunks=bool(raw["keep_local_chunks"]),
            min_free_gb=float(raw["min_free_gb"]),
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

    def chunk_path(self, name: str) -> Path:
        return self.backup_dir / name

    def remote_path(self, name: str) -> str:
        return f"{self.remote_folder}/{name}"


@dataclass
class Manifest:
    """State file. `files` maps arcname -> file entry, `chunks` maps chunk
    zip name -> chunk entry; a file entry's `chunk` key names its chunk."""

    path: Path
    last_backup: str = ""
    last_upload: str = ""
    next_chunk: int = 1
    chunks: dict[str, dict] = field(default_factory=dict)
    files: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text())
        version = data.get("version", 1)
        if version != MANIFEST_VERSION:
            # v1 tracked one monolithic zip; that zip stays on disk untouched,
            # so nothing is lost by starting a fresh v2 state.
            backup = path.with_suffix(f".v{version}.bak.json")
            shutil.copy2(path, backup)
            log.warning(
                "Manifest version %s is not supported by the chunked format; "
                "starting fresh. Old manifest kept at %s", version, backup,
            )
            return cls(path=path)
        return cls(
            path=path,
            last_backup=data.get("last_backup", ""),
            last_upload=data.get("last_upload", ""),
            next_chunk=data.get("next_chunk", 1),
            chunks=data.get("chunks", {}),
            files=data.get("files", {}),
        )

    def save(self) -> None:
        payload = {
            "version": MANIFEST_VERSION,
            "last_backup": self.last_backup,
            "last_upload": self.last_upload,
            "next_chunk": self.next_chunk,
            "chunks": self.chunks,
            "files": self.files,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def new_chunk_name(self, kind: str) -> str:
        name = f"{'sync' if kind == 'sync' else 'arch'}-{self.next_chunk:05d}.zip"
        self.next_chunk += 1
        return name


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


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


class BackupError(Exception):
    """A fatal, already-logged error; the message is user-facing."""


# --------------------------------------------------------------------------- #
# Scanning & diffing
# --------------------------------------------------------------------------- #

@dataclass
class Member:
    """One file destined for (or already inside) a chunk."""
    arcname: str
    origin: Path
    size: int
    mtime_ns: int
    sha256: str = ""
    source: str = "sync"

    @classmethod
    def from_entry(cls, arcname: str, entry: dict) -> "Member":
        return cls(arcname, Path(entry["origin"]), entry["size"],
                   entry["mtime_ns"], entry["sha256"], entry["source"])

    def to_entry(self, chunk: str) -> dict:
        return {"size": self.size, "mtime_ns": self.mtime_ns, "sha256": self.sha256,
                "source": self.source, "origin": str(self.origin), "chunk": chunk}


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
    added: list[Member] = field(default_factory=list)
    changed: list[Member] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # arcnames
    touched: list[Member] = field(default_factory=list)  # stat changed, hash identical


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
            m = Member(arcname, path, st.st_size, st.st_mtime_ns)
            m.sha256 = sha256_file(path)
            if entry is None:
                diff.added.append(m)
            elif entry["sha256"] == m.sha256:
                diff.touched.append(m)  # metadata refresh only, no chunk rebuild
            else:
                diff.changed.append(m)

    active_prefixes = tuple(f"Sync/{d.name}/" for d in cfg.sync_dirs if d.is_dir())
    diff.deleted = [
        arc
        for arc, entry in manifest.files.items()
        if entry.get("source") == "sync"
        and arc.startswith(active_prefixes)
        and arc not in seen
    ]
    return diff


def scan_dropzone(cfg: Config, manifest: Manifest) -> tuple[list[Member], dict[Path, list[str]]]:
    """Collect dropzone files ready for archiving.

    Returns the members plus a map of top-level dropzone entries to the
    arcnames they contain, so an entry is only trashed once every file in it
    is confirmed uploaded.
    """
    pending: list[Member] = []
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
            if prior and prior["sha256"] == digest:
                chunk = manifest.chunks.get(prior.get("chunk", ""))
                if chunk and (chunk["uploaded"] or cfg.chunk_path(prior["chunk"]).is_file()):
                    # Identical content is already in a chunk (uploaded, or
                    # built and awaiting upload): don't archive it again.
                    group.append(arcname)
                    continue
            if prior and prior["sha256"] != digest:
                # Same name, different content: keep both by timestamping the new one.
                stem, dot, ext = arcname.rpartition(".")
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                arcname = f"{stem}_{stamp}.{ext}" if dot else f"{arcname}_{stamp}"
                while arcname in existing_names:
                    arcname += "_1"
            existing_names.add(arcname)
            pending.append(Member(arcname, path, st.st_size, st.st_mtime_ns,
                                  digest, source="dropzone"))
            group.append(arcname)
        groups[top] = group
    return pending, groups


# --------------------------------------------------------------------------- #
# Chunk planning & building
# --------------------------------------------------------------------------- #

@dataclass
class ChunkPlan:
    name: str
    kind: str  # "sync" | "archive"
    members: list[Member]

    @property
    def total_bytes(self) -> int:
        return sum(m.size for m in self.members)


def pack_new_members(members: list[Member], kind: str, cfg: Config,
                     manifest: Manifest) -> list[ChunkPlan]:
    """First-fit pack new files into chunks of at most chunk_cap bytes.

    A file larger than the cap is never split: it gets a dedicated chunk of
    its own (one zip, one file), so a 4 GB video is one self-contained chunk.
    """
    plans: list[ChunkPlan] = []
    current: list[Member] = []
    current_bytes = 0
    for m in sorted(members, key=lambda m: m.arcname):
        if m.size >= cfg.chunk_cap:
            plans.append(ChunkPlan(manifest.new_chunk_name(kind), kind, [m]))
            continue
        if current and current_bytes + m.size > cfg.chunk_cap:
            plans.append(ChunkPlan(manifest.new_chunk_name(kind), kind, current))
            current, current_bytes = [], 0
        current.append(m)
        current_bytes += m.size
    if current:
        plans.append(ChunkPlan(manifest.new_chunk_name(kind), kind, current))
    return plans


def plan_rebuilds(cfg: Config, manifest: Manifest, diff: SyncDiff) -> tuple[list[ChunkPlan], list[str]]:
    """Plan rebuilds for sync chunks with a changed or deleted member.

    Sync chunks are rebuilt entirely from the live local files, so updating a
    chunk never requires downloading anything. Returns (rebuild plans, chunks
    that end up empty and must be removed).
    """
    dirty: set[str] = set()
    for arc in diff.deleted:
        dirty.add(manifest.files[arc]["chunk"])
    changed_by_arc = {m.arcname: m for m in diff.changed}
    for m in diff.changed:
        dirty.add(manifest.files[m.arcname]["chunk"])

    plans: list[ChunkPlan] = []
    empty: list[str] = []
    deleted = set(diff.deleted)
    for chunk_name in sorted(dirty):
        members = [
            changed_by_arc.get(arc) or Member.from_entry(arc, entry)
            for arc, entry in manifest.files.items()
            if entry.get("chunk") == chunk_name and arc not in deleted
        ]
        if members:
            plans.append(ChunkPlan(chunk_name, "sync", members))
        else:
            empty.append(chunk_name)
    return plans, empty


def check_free_space(cfg: Config, needed_bytes: int) -> None:
    margin = int(cfg.min_free_gb * 1024**3)
    free = shutil.disk_usage(cfg.backup_dir).free
    if free < needed_bytes + margin:
        raise BackupError(
            f"Low disk space: need ~{human_size(needed_bytes + margin)} free "
            f"(including {cfg.min_free_gb} GB margin), have {human_size(free)}."
        )


def build_chunk(cfg: Config, plan: ChunkPlan) -> dict:
    """Write one chunk zip and return its manifest entry.

    Already-compressed formats are stored rather than deflated, and every
    archive-chunk member is streamed back out of the finished zip and
    hash-verified before the chunk is accepted.
    """
    path = cfg.chunk_path(plan.name)
    tmp = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for m in plan.members:
            method = (zipfile.ZIP_STORED
                      if Path(m.arcname).suffix.lower() in STORED_SUFFIXES
                      else zipfile.ZIP_DEFLATED)
            zf.write(m.origin, m.arcname, compress_type=method)
    with zipfile.ZipFile(tmp) as zf:
        bad = zf.testzip()
        if bad is not None:
            tmp.unlink(missing_ok=True)
            raise BackupError(f"Chunk {plan.name} failed its CRC check at {bad}.")
        if plan.kind == "archive":
            for m in plan.members:
                if sha256_zip_entry(zf, m.arcname) != m.sha256:
                    tmp.unlink(missing_ok=True)
                    raise BackupError(
                        f"Hash mismatch inside {plan.name} for {m.arcname}; "
                        "the original was NOT touched."
                    )
    os.replace(tmp, path)
    return {
        "kind": plan.kind,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "files": len(plan.members),
        "uploaded": "",
    }


def reconcile_chunks(cfg: Config, manifest: Manifest) -> set[str]:
    """Handle chunks that were built earlier but vanished before upload.

    Sync chunks are simply rebuilt from the live files; archive chunks are
    dropped from the manifest — their originals are still in the dropzone
    (they are only trashed after a confirmed upload), so the next scan
    re-archives them. Returns sync chunk names needing a rebuild.
    """
    rebuild: set[str] = set()
    for name, meta in list(manifest.chunks.items()):
        if meta["uploaded"] or cfg.chunk_path(name).is_file():
            continue
        if meta["kind"] == "sync":
            log.warning("Chunk %s disappeared before upload; will rebuild.", name)
            rebuild.add(name)
        else:
            log.warning(
                "Archive chunk %s disappeared before upload; dropping it — "
                "its originals are still in the dropzone and will be re-archived.",
                name,
            )
            manifest.files = {
                arc: e for arc, e in manifest.files.items() if e.get("chunk") != name
            }
            del manifest.chunks[name]
    return rebuild


# --------------------------------------------------------------------------- #
# Proton Drive
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


def proton(cfg: Config, *args: str, timeout: int = SUBPROCESS_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cfg.proton_cli, *args], capture_output=True, text=True, timeout=timeout
    )


def ensure_remote_folder(cfg: Config) -> None:
    """Create each component of remote_folder; 'already exists' is fine.

    The first component is a namespace root (/my-files, ...) that always
    exists and cannot be created, so folder creation starts below it.
    """
    parts = [p for p in cfg.remote_folder.split("/") if p]
    parent = "/" + parts[0]
    for part in parts[1:]:
        proc = proton(cfg, "filesystem", "create-folder", parent, part)
        if proc.returncode != 0 and "exist" not in (proc.stderr + proc.stdout).lower():
            log.debug("create-folder %s/%s: %s", parent.rstrip("/"), part, proc.stderr.strip())
        parent = parent.rstrip("/") + "/" + part


def upload_file(cfg: Config, local: Path) -> None:
    proc = proton(
        cfg, "filesystem", "upload", "-c", "replace", "-t",
        str(local), cfg.remote_folder, timeout=TRANSFER_TIMEOUT,
    )
    if proc.returncode != 0:
        output = proc.stderr + proc.stdout
        raise UploadError(classify_upload_error(output), output.strip()[:500] or "unknown error")


def confirm_remote(cfg: Config, name: str, expect_size: int, expect_sha1: str) -> bool:
    """Check the uploaded file's remote metadata against the local chunk.

    `filesystem info -j` reports the plaintext size (claimedSize) and a SHA-1
    digest claimed at upload time; both must match before the local copy may
    be deleted. Returns False only on a *positive* mismatch; an unreadable or
    unparseable response is trusted (the upload already exited 0)."""
    try:
        proc = proton(cfg, "filesystem", "info", "-j", cfg.remote_path(name))
        if proc.returncode != 0:
            log.debug("Remote confirm of %s unavailable: %s", name, proc.stderr.strip())
            return True
        sizes: list[int] = []
        sha1s: list[str] = []

        def collect(node) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if "size" in k.lower() and isinstance(v, int):
                        sizes.append(v)
                    elif k == "sha1" and isinstance(v, str):
                        sha1s.append(v.lower())
                    collect(v)
            elif isinstance(node, list):
                for v in node:
                    collect(v)

        collect(json.loads(proc.stdout))
        if sizes and expect_size not in sizes:
            log.error("Remote size mismatch for %s: local %d, remote %s", name, expect_size, sizes)
            return False
        if sha1s and expect_sha1 not in sha1s:
            log.error("Remote SHA-1 mismatch for %s: local %s, remote %s", name, expect_sha1, sha1s)
            return False
        return True
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return True


def download_chunk(cfg: Config, name: str, dest_dir: Path) -> Path:
    proc = proton(
        cfg, "filesystem", "download", "-c", "replace",
        cfg.remote_path(name), str(dest_dir), timeout=TRANSFER_TIMEOUT,
    )
    local = dest_dir / name
    if proc.returncode != 0 or not local.is_file():
        output = (proc.stderr + proc.stdout).strip()[:500]
        raise UploadError(classify_upload_error(output), f"download of {name} failed: {output}")
    return local


def upload_pending(cfg: Config, manifest: Manifest,
                   trash_remote: list[str]) -> tuple[int, int, UploadError | None]:
    """Upload every not-yet-uploaded chunk plus the manifest.

    After each confirmed chunk upload the local zip is deleted (unless
    keep_local_chunks). Returns (chunks uploaded, local bytes freed, error).
    """
    error: UploadError | None = None
    uploaded = 0
    freed = 0
    try:
        ensure_remote_folder(cfg)
        for name in sorted(manifest.chunks):
            meta = manifest.chunks[name]
            local = cfg.chunk_path(name)
            if meta["uploaded"] or not local.is_file():
                continue
            log.info("Uploading %s (%s) ...", name, human_size(meta["size"]))
            with open(local, "rb") as fh:
                local_sha1 = hashlib.file_digest(fh, "sha1").hexdigest()
            upload_file(cfg, local)
            if not confirm_remote(cfg, name, meta["size"], local_sha1):
                raise UploadError("error", f"remote verification failed for {name}")
            meta["uploaded"] = now_iso()
            uploaded += 1
            manifest.save()  # persist progress after every chunk
            if not cfg.keep_local_chunks:
                freed += meta["size"]
                local.unlink()
    except UploadError as exc:
        error = exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = UploadError("error", str(exc))

    if error is None:
        # Old chunks are only trashed remotely once their replacements are up.
        for name in trash_remote:
            proc = proton(cfg, "filesystem", "trash", cfg.remote_path(name))
            if proc.returncode != 0:
                log.warning("Could not trash remote %s: %s", name, proc.stderr.strip())
        try:
            manifest.save()
            upload_file(cfg, manifest.path)
            manifest.last_upload = now_iso()
        except UploadError as exc:
            error = exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = UploadError("error", str(exc))
    manifest.save()
    return uploaded, freed, error


UPLOAD_FAIL_MESSAGES = {
    "quota": "Proton Drive storage is FULL — upload failed. Free up space in your plan.",
    "auth": "Proton Drive login expired — run `proton-drive auth login`.",
    "error": "Proton Drive upload failed (network/CLI error). Data is kept locally.",
}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_backup(cfg: Config, do_upload: bool, dry_run: bool, notify_enabled: bool) -> int:
    manifest = Manifest.load(cfg.manifest_path)
    forced_rebuilds = reconcile_chunks(cfg, manifest)

    diff = diff_sync_dirs(cfg, manifest)
    dropzone_members, dropzone_groups = scan_dropzone(cfg, manifest)

    rebuild_plans, emptied_chunks = plan_rebuilds(cfg, manifest, diff)
    planned = {p.name for p in rebuild_plans}
    for name in forced_rebuilds - planned:
        members = [Member.from_entry(arc, e) for arc, e in manifest.files.items()
                   if e.get("chunk") == name]
        if members:
            rebuild_plans.append(ChunkPlan(name, "sync", members))
    new_sync_plans = pack_new_members(diff.added, "sync", cfg, manifest)
    new_arch_plans = pack_new_members(dropzone_members, "archive", cfg, manifest)
    all_plans = rebuild_plans + new_sync_plans + new_arch_plans

    log.info(
        "Diff: %d added, %d changed, %d deleted, %d touched, %d dropzone file(s) "
        "-> %d chunk build(s), %d chunk removal(s).",
        len(diff.added), len(diff.changed), len(diff.deleted), len(diff.touched),
        len(dropzone_members), len(all_plans), len(emptied_chunks),
    )

    if dry_run:
        for label, items in (
            ("ADD", [m.arcname for m in diff.added]),
            ("UPDATE", [m.arcname for m in diff.changed]),
            ("DELETE", diff.deleted),
            ("ARCHIVE", [m.arcname for m in dropzone_members]),
        ):
            for arc in items:
                print(f"{label:8} {arc}")
        for plan in all_plans:
            print(f"CHUNK    {plan.name} ({plan.kind}, {len(plan.members)} file(s), "
                  f"{human_size(plan.total_bytes)})")
        for name in emptied_chunks:
            print(f"REMOVE   {name} (no members left)")
        print("\nNothing was modified.")
        return 0

    # ----- build chunks --------------------------------------------------- #
    if all_plans:
        check_free_space(cfg, sum(p.total_bytes for p in all_plans))
        for plan in all_plans:
            log.info("Building %s (%d file(s), %s)...",
                     plan.name, len(plan.members), human_size(plan.total_bytes))
            manifest.chunks[plan.name] = build_chunk(cfg, plan)

    # ----- update manifest ------------------------------------------------ #
    for arc in diff.deleted:
        manifest.files.pop(arc, None)
    trash_remote: list[str] = []
    for name in emptied_chunks:
        meta = manifest.chunks.pop(name, None)
        cfg.chunk_path(name).unlink(missing_ok=True)
        if meta and meta["uploaded"]:
            trash_remote.append(name)
    for plan in all_plans:
        for m in plan.members:
            manifest.files[m.arcname] = m.to_entry(plan.name)
    for m in diff.touched:
        chunk = manifest.files[m.arcname]["chunk"]
        manifest.files[m.arcname] = m.to_entry(chunk)
    manifest.last_backup = now_iso()
    manifest.save()

    # ----- upload, then free local space ----------------------------------- #
    uploaded = freed = 0
    upload_error: UploadError | None = None
    pending_upload = any(
        not meta["uploaded"] and cfg.chunk_path(name).is_file()
        for name, meta in manifest.chunks.items()
    )
    if do_upload and (pending_upload or all_plans or trash_remote or diff.deleted or diff.touched):
        uploaded, freed, upload_error = upload_pending(cfg, manifest, trash_remote)
        if upload_error:
            log.error("Upload failed (%s): %s", upload_error.kind, upload_error)

    # ----- trash dropzone originals whose chunks are confirmed uploaded ---- #
    trashed = 0
    waiting = 0
    for top, arcnames in dropzone_groups.items():
        fully_uploaded = all(
            a in manifest.files
            and manifest.chunks.get(manifest.files[a]["chunk"], {}).get("uploaded")
            for a in arcnames
        )
        if fully_uploaded:
            if move_to_trash(top):
                trashed += 1
                log.info("Uploaded and trashed dropzone entry: %s", top.name)
        else:
            waiting += 1
            log.info("Dropzone entry kept until its upload is confirmed: %s", top.name)

    # ----- report ---------------------------------------------------------- #
    if upload_error:
        notify(APP_NAME, UPLOAD_FAIL_MESSAGES[upload_error.kind], notify_enabled)
        return 1
    parts = [f"{len(diff.added)} added, {len(diff.changed)} updated, {len(diff.deleted)} removed"]
    if trashed or waiting:
        parts.append(f"{trashed} dropzone item(s) offloaded")
    if uploaded:
        parts.append(f"{uploaded} chunk(s) uploaded")
    if freed:
        parts.append(f"{human_size(freed)} freed locally")
    notify(APP_NAME, "Backup OK — " + ", ".join(parts) + ".", notify_enabled)
    return 0


def cmd_status(cfg: Config) -> int:
    manifest = Manifest.load(cfg.manifest_path)
    total = len(manifest.files)
    total_bytes = sum(e["size"] for e in manifest.files.values())
    sync_n = sum(1 for e in manifest.files.values() if e.get("source") == "sync")
    arch_n = total - sync_n
    n_chunks = len(manifest.chunks)
    pending_chunks = [n for n, m in manifest.chunks.items() if not m["uploaded"]]
    local_bytes = sum(
        cfg.chunk_path(n).stat().st_size
        for n in manifest.chunks if cfg.chunk_path(n).is_file()
    )
    pending = 0
    if cfg.dropzone.is_dir():
        pending = sum(
            1 for p in cfg.dropzone.iterdir()
            if not p.name.startswith(".") and not cfg.is_excluded(p.name)
        )

    lines = [
        f"{APP_NAME} status",
        "-" * 36,
        f"Files backed up:    {total}  ({sync_n} sync, {arch_n} archived)",
        f"Total size:         {human_size(total_bytes)}",
        f"Chunks:             {n_chunks} on {cfg.remote_folder}",
        f"Awaiting upload:    {len(pending_chunks)} chunk(s)"
        + (f" ({', '.join(sorted(pending_chunks))})" if pending_chunks else ""),
        f"Local chunk cache:  {human_size(local_bytes)}",
        f"Last backup:        {manifest.last_backup or 'never'}",
        f"Last upload:        {manifest.last_upload or 'never'}",
        f"Dropzone pending:   {pending} item(s)",
    ]
    print("\n".join(lines))
    return 0


def cmd_advice(cfg: Config, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"Not a directory: {target}", file=sys.stderr)
        return 2
    manifest = Manifest.load(cfg.manifest_path)
    if not manifest.files:
        print("Nothing has been backed up yet — nothing is safe to delete.")
        return 1

    # Only entries whose chunk is confirmed uploaded count as safe: once local
    # chunks are deleted, Proton Drive is the *only* copy.
    def is_safe(entry: dict) -> bool:
        return bool(manifest.chunks.get(entry.get("chunk", ""), {}).get("uploaded"))

    by_hash = {e["sha256"]: arc for arc, e in manifest.files.items() if is_safe(e)}
    by_origin = {e["origin"]: (arc, e) for arc, e in manifest.files.items() if is_safe(e)}

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
    print(f"(verified against chunks uploaded to {cfg.remote_folder}, "
          f"last backup {manifest.last_backup})")
    print()
    if safe:
        print(f"SAFE TO DELETE — {len(safe)} file(s), byte-identical copies are uploaded:")
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


def cmd_restore(cfg: Config, pattern: str, dest: Path) -> int:
    """Restore files by name/glob, downloading only the chunks that hold them."""
    manifest = Manifest.load(cfg.manifest_path)
    matches = sorted(
        arc for arc in manifest.files
        if fnmatch.fnmatchcase(arc, pattern) or pattern.lower() in arc.lower()
    )
    if not matches:
        print(f"No backed-up file matches {pattern!r}. Try --status or a broader pattern.")
        return 1
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    by_chunk: dict[str, list[str]] = {}
    for arc in matches:
        by_chunk.setdefault(manifest.files[arc]["chunk"], []).append(arc)
    print(f"Restoring {len(matches)} file(s) from {len(by_chunk)} chunk(s) to {dest}")

    failures = 0
    with tempfile.TemporaryDirectory(dir=cfg.backup_dir if cfg.backup_dir.is_dir() else None) as tmp:
        for chunk_name, arcs in sorted(by_chunk.items()):
            local = cfg.chunk_path(chunk_name)
            if not local.is_file():
                print(f"  downloading {chunk_name} "
                      f"({human_size(manifest.chunks.get(chunk_name, {}).get('size', 0))}) ...")
                try:
                    local = download_chunk(cfg, chunk_name, Path(tmp))
                except UploadError as exc:
                    print(f"  ERROR: {exc}", file=sys.stderr)
                    failures += len(arcs)
                    continue
            with zipfile.ZipFile(local) as zf:
                for arc in arcs:
                    out = dest / arc
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(arc) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    if sha256_file(out) == manifest.files[arc]["sha256"]:
                        print(f"  OK  {arc}")
                    else:
                        failures += 1
                        print(f"  HASH MISMATCH  {arc} (restored file kept for inspection)",
                              file=sys.stderr)
    if failures:
        print(f"\n{failures} file(s) failed to restore.", file=sys.stderr)
        return 1
    print("\nAll files restored and hash-verified.")
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
    parser.add_argument("--restore", metavar="NAME",
                        help="restore file(s) matching a name, substring or glob")
    parser.add_argument("--dest", metavar="DIR", type=Path,
                        default=Path("~/Downloads/BackupOrganizer-Restore"),
                        help="destination for --restore (default: %(default)s)")
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
    if args.restore:
        return cmd_restore(cfg, args.restore, args.dest)

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
