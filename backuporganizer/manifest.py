"""The manifest state file and the Member record that mirrors its entries."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .util import log

MANIFEST_VERSION = 3


def _migrate_v2_to_v3(data: dict) -> dict:
    """Strip sync chunks/entries from a v2 manifest; sync data re-uploads
    as plain files from its still-present local originals (never deleted
    by this tool). Old sync-*.zip chunks that were confirmed uploaded are
    queued in pending_remote_cleanup so the next successful upload trashes
    them remotely instead of leaving them orphaned."""
    chunks = data.get("chunks", {})
    files = data.get("files", {})
    sync_chunk_names = {name for name, meta in chunks.items() if meta.get("kind") == "sync"}
    cleanup = sorted(
        name for name in sync_chunk_names if chunks[name].get("uploaded")
    )
    kept_chunks = {name: meta for name, meta in chunks.items() if name not in sync_chunk_names}
    kept_files = {arc: e for arc, e in files.items() if e.get("source") != "sync"}
    n_sync_files = len(files) - len(kept_files)
    log.warning(
        "Migrating manifest v2 -> v3: %d sync file(s) will re-upload as plain "
        "mirrored files (their local originals are untouched), %d legacy sync "
        "chunk(s) queued for remote cleanup on the next successful upload. "
        "Nothing is written to disk until the next `run`.",
        n_sync_files, len(cleanup),
    )
    return {
        **data,
        "chunks": kept_chunks,
        "files": kept_files,
        "pending_remote_cleanup": cleanup,
        "remote_dirs": [],
        "deleted_sync": {},
    }


@dataclass
class Manifest:
    """State file. `files` maps arcname -> file entry, `chunks` maps archive
    zip name -> chunk entry; an archive file entry's `chunk` key names its
    chunk. Sync file entries have no `chunk` (they are plain mirrored remote
    files) and carry their own `uploaded` timestamp instead.

    Written atomically (temp file + os.replace) and re-saved after every
    uploaded file/chunk, so an interrupted run resumes where it stopped.
    """

    path: Path
    last_backup: str = ""
    last_upload: str = ""
    next_chunk: int = 1
    chunks: dict[str, dict] = field(default_factory=dict)
    files: dict[str, dict] = field(default_factory=dict)
    remote_dirs: list[str] = field(default_factory=list)
    pending_remote_cleanup: list[str] = field(default_factory=list)
    deleted_sync: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Load the manifest, or return an empty one for first runs.

        A v2 (chunked-sync) manifest is migrated in place (see
        _migrate_v2_to_v3): the old file is backed up untouched, and sync
        data is dropped so it re-uploads as plain mirrored files. An older,
        unsupported (v1, monolithic-zip) manifest is backed up and a fresh
        v3 state is started instead, matching the original precedent.
        """
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text())
        version = data.get("version", 1)
        if version == MANIFEST_VERSION:
            return cls(
                path=path,
                last_backup=data.get("last_backup", ""),
                last_upload=data.get("last_upload", ""),
                next_chunk=data.get("next_chunk", 1),
                chunks=data.get("chunks", {}),
                files=data.get("files", {}),
                remote_dirs=data.get("remote_dirs", []),
                pending_remote_cleanup=data.get("pending_remote_cleanup", []),
                deleted_sync=data.get("deleted_sync", {}),
            )
        backup = path.with_suffix(f".v{version}.bak.json")
        shutil.copy2(path, backup)
        if version == 2:
            data = _migrate_v2_to_v3(data)
            return cls(
                path=path,
                last_backup=data.get("last_backup", ""),
                last_upload=data.get("last_upload", ""),
                next_chunk=data.get("next_chunk", 1),
                chunks=data.get("chunks", {}),
                files=data.get("files", {}),
                remote_dirs=data.get("remote_dirs", []),
                pending_remote_cleanup=data.get("pending_remote_cleanup", []),
                deleted_sync=data.get("deleted_sync", {}),
            )
        log.warning(
            "Manifest version %s is not supported; starting fresh. "
            "Old manifest kept at %s", version, backup,
        )
        return cls(path=path)

    def save(self) -> None:
        """Atomically write the manifest to disk."""
        payload = {
            "version": MANIFEST_VERSION,
            "last_backup": self.last_backup,
            "last_upload": self.last_upload,
            "next_chunk": self.next_chunk,
            "chunks": self.chunks,
            "files": self.files,
            "remote_dirs": self.remote_dirs,
            "pending_remote_cleanup": self.pending_remote_cleanup,
            "deleted_sync": self.deleted_sync,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def new_chunk_name(self) -> str:
        """Reserve the next sequential archive chunk name (arch-00001.zip)."""
        name = f"arch-{self.next_chunk:05d}.zip"
        self.next_chunk += 1
        return name


@dataclass
class Member:
    """One file destined for (or already inside) a chunk, or a plain
    mirrored sync upload."""

    arcname: str
    origin: Path
    size: int
    mtime_ns: int
    sha256: str = ""
    source: str = "sync"

    @classmethod
    def from_entry(cls, arcname: str, entry: dict) -> "Member":
        """Rebuild a Member from its manifest file entry."""
        return cls(arcname, Path(entry["origin"]), entry["size"],
                   entry["mtime_ns"], entry["sha256"], entry["source"])

    def to_entry_archive(self, chunk: str) -> dict:
        """The manifest file entry for an archive member, assigned to `chunk`."""
        return {"size": self.size, "mtime_ns": self.mtime_ns, "sha256": self.sha256,
                "source": self.source, "origin": str(self.origin), "chunk": chunk}

    def to_entry_sync(self, uploaded: str) -> dict:
        """The manifest file entry for a plain mirrored sync upload."""
        return {"size": self.size, "mtime_ns": self.mtime_ns, "sha256": self.sha256,
                "source": self.source, "origin": str(self.origin), "uploaded": uploaded}
