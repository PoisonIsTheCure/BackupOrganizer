"""Planning, building, repacking and verifying chunk zips."""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .manifest import Manifest, Member
from .proton import download_chunk, proton
from .util import BackupError, human_size, log, sha256_file, sha256_zip_entry

# Already-compressed formats are stored, not deflated: recompressing a video
# burns minutes of CPU for well under 1% size gain.
STORED_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg",
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".dmg",
}


@dataclass
class ChunkPlan:
    """A chunk to build this run: its name, kind, and member files."""

    name: str
    kind: str  # "sync" | "archive"
    members: list[Member]

    @property
    def total_bytes(self) -> int:
        """Uncompressed size of all members (worst-case space estimate)."""
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


def plan_rebuilds(cfg: Config, manifest: Manifest, deleted: list[str],
                  changed: list[Member]) -> tuple[list[ChunkPlan], list[str]]:
    """Plan rebuilds for sync chunks with a changed or deleted member.

    Sync chunks are rebuilt entirely from the live local files, so updating a
    chunk never requires downloading anything. Returns (rebuild plans, chunks
    that end up empty and must be removed).
    """
    dirty: set[str] = set()
    for arc in deleted:
        dirty.add(manifest.files[arc]["chunk"])
    changed_by_arc = {m.arcname: m for m in changed}
    for m in changed:
        dirty.add(manifest.files[m.arcname]["chunk"])

    plans: list[ChunkPlan] = []
    empty: list[str] = []
    deleted_set = set(deleted)
    for chunk_name in sorted(dirty):
        members = [
            changed_by_arc.get(arc) or Member.from_entry(arc, entry)
            for arc, entry in manifest.files.items()
            if entry.get("chunk") == chunk_name and arc not in deleted_set
        ]
        if members:
            plans.append(ChunkPlan(chunk_name, "sync", members))
        else:
            empty.append(chunk_name)
    return plans, empty


def check_free_space(cfg: Config, needed_bytes: int) -> None:
    """Abort (BackupError) unless needed_bytes + the margin fit on disk."""
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


def repack_archive_chunk(cfg: Config, manifest: Manifest, chunk_name: str,
                         remove: set[str]) -> None:
    """Rebuild an archive chunk without the removed entries.

    Downloads the chunk first if no local copy exists. The rebuilt chunk is
    hash-verified and left pending upload (same name, replaced remotely on
    the next upload); a chunk left with no entries is deleted outright.
    """
    meta = manifest.chunks[chunk_name]
    keep = [a for a, e in manifest.files.items()
            if e.get("chunk") == chunk_name and a not in remove]
    was_uploaded = bool(meta["uploaded"])
    local = cfg.chunk_path(chunk_name)
    with tempfile.TemporaryDirectory(dir=cfg.backup_dir) as tmp:
        src = local if local.is_file() else download_chunk(cfg, chunk_name, Path(tmp))
        if keep:
            new = Path(tmp) / f"repack_{chunk_name}"
            with zipfile.ZipFile(src) as zin, zipfile.ZipFile(new, "w") as zout:
                for arc in keep:
                    info = zin.getinfo(arc)
                    zi = zipfile.ZipInfo(arc, date_time=info.date_time)
                    zi.compress_type = info.compress_type
                    with zin.open(arc) as r, zout.open(zi, mode="w") as w:
                        shutil.copyfileobj(r, w, 1024 * 1024)
            with zipfile.ZipFile(new) as zf:
                if zf.testzip() is not None:
                    raise BackupError(f"Repacked {chunk_name} failed its CRC check.")
                for arc in keep:
                    if sha256_zip_entry(zf, arc) != manifest.files[arc]["sha256"]:
                        raise BackupError(
                            f"Verification failed while repacking {chunk_name}; "
                            "nothing was removed."
                        )
            os.replace(new, local)
            meta.update(size=local.stat().st_size, sha256=sha256_file(local),
                        files=len(keep), uploaded="")
        else:
            local.unlink(missing_ok=True)
            del manifest.chunks[chunk_name]
    for arc in remove:
        manifest.files.pop(arc, None)
    if not keep and was_uploaded:
        proc = proton(cfg, "filesystem", "trash", cfg.remote_path(chunk_name))
        if proc.returncode != 0:
            log.warning("Could not trash remote %s: %s", chunk_name, proc.stderr.strip())
    manifest.save()
