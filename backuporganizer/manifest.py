"""The manifest state file and the Member record that mirrors its entries."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .util import log

MANIFEST_VERSION = 2


@dataclass
class Manifest:
    """State file. `files` maps arcname -> file entry, `chunks` maps chunk
    zip name -> chunk entry; a file entry's `chunk` key names its chunk.

    Written atomically (temp file + os.replace) and re-saved after every
    uploaded chunk, so an interrupted run resumes where it stopped.
    """

    path: Path
    last_backup: str = ""
    last_upload: str = ""
    next_chunk: int = 1
    chunks: dict[str, dict] = field(default_factory=dict)
    files: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Load the manifest, or return an empty one for first runs.
        An unsupported (v1, monolithic-zip) manifest is backed up and a
        fresh v2 state is started — the old zip on disk is untouched."""
        if not path.is_file():
            return cls(path=path)
        data = json.loads(path.read_text())
        version = data.get("version", 1)
        if version != MANIFEST_VERSION:
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
        """Atomically write the manifest to disk."""
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
        """Reserve the next sequential chunk name (sync-00001.zip / arch-...)."""
        name = f"{'sync' if kind == 'sync' else 'arch'}-{self.next_chunk:05d}.zip"
        self.next_chunk += 1
        return name


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
        """Rebuild a Member from its manifest file entry."""
        return cls(arcname, Path(entry["origin"]), entry["size"],
                   entry["mtime_ns"], entry["sha256"], entry["source"])

    def to_entry(self, chunk: str) -> dict:
        """The manifest file entry for this member, assigned to `chunk`."""
        return {"size": self.size, "mtime_ns": self.mtime_ns, "sha256": self.sha256,
                "source": self.source, "origin": str(self.origin), "chunk": chunk}
