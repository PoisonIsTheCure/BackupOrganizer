"""Walking sync dirs and the dropzone, and diffing them against the manifest."""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .manifest import Manifest, Member
from .util import log, sha256_file

# Files newer than this many seconds are skipped in the dropzone so we never
# archive a copy that is still being written.
DROPZONE_SETTLE_SECONDS = 30


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
    """Classified changes between the sync dirs on disk and the manifest."""

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


def scan_dropzone(cfg: Config, manifest: Manifest,
                  settle_seconds: int = DROPZONE_SETTLE_SECONDS) -> tuple[
        list[Member], dict[Path, list[str]], dict[str, list[dict]]]:
    """Collect dropzone files ready for archiving.

    Returns (members, groups, relocations):
    - members: files to pack into new archive chunks this run.
    - groups maps top-level dropzone entries to the arcnames they contain, so
      an entry is only trashed once every file in it is confirmed uploaded.
    - relocations maps a new archive arcname to the *sync* manifest entries
      holding identical content: the dropped file was copied out of a synced
      area, so once the archive copy is confirmed uploaded the sync-area
      original is trashed too — only the archived copy stays, no duplicate.
      (Only populated when cfg.retire_sync_copies is enabled.)
    Content already archived under another name is not stored twice: the
    dropped file just joins its group and is trashed once that chunk is up.
    """
    pending: list[Member] = []
    groups: dict[Path, list[str]] = {}
    relocations: dict[str, list[dict]] = {}
    if not cfg.dropzone.is_dir():
        return pending, groups, relocations

    cutoff = datetime.datetime.now().timestamp() - settle_seconds
    existing_names = set(manifest.files)
    by_content: dict[str, list[tuple[str, dict]]] = {}
    for a, e in manifest.files.items():
        by_content.setdefault(e["sha256"], []).append((a, e))

    def safely_stored(entry: dict) -> bool:
        chunk = manifest.chunks.get(entry.get("chunk", ""))
        return bool(chunk and (chunk["uploaded"] or cfg.chunk_path(entry["chunk"]).is_file()))

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
            matches = by_content.get(digest, [])
            # Sync entries with identical content: the file was copied out of
            # a synced area, so those originals are retired once the archive
            # copy is confirmed uploaded — recorded on every path, because
            # the confirmation may only happen on a later run.
            sync_twins = ([dict(e) for a, e in matches if e["source"] == "sync"]
                          if cfg.retire_sync_copies else [])
            if prior and prior["sha256"] == digest and safely_stored(prior):
                # Identical content is already in a chunk (uploaded, or built
                # and awaiting upload): don't archive it again.
                group.append(arcname)
                if sync_twins:
                    relocations[arcname] = sync_twins
                continue
            arch_twin = next(
                (a for a, e in matches
                 if e["source"] == "dropzone" and a != arcname and safely_stored(e)),
                None,
            )
            if arch_twin:
                log.info("Dropzone %s is already archived as %s; not storing it twice.",
                         path.name, arch_twin)
                group.append(arch_twin)
                if sync_twins:
                    relocations[arch_twin] = sync_twins
                continue
            if prior and prior["sha256"] != digest:
                # Same name, different content: keep both by timestamping the new one.
                stem, dot, ext = arcname.rpartition(".")
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                arcname = f"{stem}_{stamp}.{ext}" if dot else f"{arcname}_{stamp}"
                while arcname in existing_names:
                    arcname += "_1"
            if sync_twins:
                relocations[arcname] = sync_twins
            existing_names.add(arcname)
            pending.append(Member(arcname, path, st.st_size, st.st_mtime_ns,
                                  digest, source="dropzone"))
            group.append(arcname)
        groups[top] = group
    return pending, groups, relocations
