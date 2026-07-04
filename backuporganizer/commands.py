"""Top-level operations: backup, status, advice, restore, dedupe, init."""

from __future__ import annotations

import fnmatch
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .chunks import (ChunkPlan, build_chunk, check_free_space, pack_new_members,
                     plan_rebuilds, reconcile_chunks, repack_archive_chunk)
from .config import DEFAULT_CONFIG, Config
from .manifest import Manifest, Member
from .proton import UPLOAD_FAIL_MESSAGES, UploadError, download_chunk, upload_pending
from .scanner import diff_sync_dirs, scan_dropzone, walk_files
from .util import (APP_NAME, human_size, log, move_to_trash, notify, now_iso,
                   sha256_file)


def cmd_backup(cfg: Config, do_upload: bool, dry_run: bool, notify_enabled: bool) -> int:
    """One full backup run: diff, build chunks, upload, free local space.

    See docs/ARCHITECTURE.md for the pipeline and its safety ordering; the
    short version is that nothing local is ever trashed before the remote
    copy of its data is confirmed.
    """
    manifest = Manifest.load(cfg.manifest_path)
    forced_rebuilds = reconcile_chunks(cfg, manifest)

    diff = diff_sync_dirs(cfg, manifest)
    dropzone_members, dropzone_groups, relocations = scan_dropzone(cfg, manifest)

    rebuild_plans, emptied_chunks = plan_rebuilds(cfg, manifest, diff.deleted, diff.changed)
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

    # ----- build chunks ---------------------------------------------------- #
    if all_plans:
        check_free_space(cfg, sum(p.total_bytes for p in all_plans))
        for plan in all_plans:
            log.info("Building %s (%d file(s), %s)...",
                     plan.name, len(plan.members), human_size(plan.total_bytes))
            manifest.chunks[plan.name] = build_chunk(cfg, plan)

    # ----- update manifest ------------------------------------------------- #
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

    # ----- upload, then free local space ------------------------------------ #
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

    # ----- trash dropzone originals whose chunks are confirmed uploaded ----- #
    trashed = 0
    waiting = 0
    relocated = 0
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
            # The dropped content was copied out of a synced area: now that
            # the archive copy is confirmed uploaded, retire the sync-area
            # original so only the archived copy remains.
            for arc in arcnames:
                for twin in relocations.get(arc, []):
                    p = Path(twin["origin"])
                    if not p.is_file():
                        continue  # was a true move, nothing left to retire
                    st = p.stat()
                    unchanged = (st.st_size == twin["size"]
                                 and st.st_mtime_ns == twin["mtime_ns"]) \
                        or sha256_file(p) == twin["sha256"]
                    if not unchanged:
                        log.warning("Sync copy of archived %s changed; keeping it: %s", arc, p)
                        continue
                    if move_to_trash(p):
                        relocated += 1
                        log.info("Retired sync copy of archived %s: %s", arc, p)
        else:
            waiting += 1
            log.info("Dropzone entry kept until its upload is confirmed: %s", top.name)

    # ----- report ------------------------------------------------------------ #
    if upload_error:
        notify(APP_NAME, UPLOAD_FAIL_MESSAGES[upload_error.kind], notify_enabled)
        return 1
    parts = [f"{len(diff.added)} added, {len(diff.changed)} updated, {len(diff.deleted)} removed"]
    if trashed or waiting:
        parts.append(f"{trashed} dropzone item(s) offloaded")
    if relocated:
        parts.append(f"{relocated} sync cop(ies) retired to archive")
    if uploaded:
        parts.append(f"{uploaded} chunk(s) uploaded")
    if freed:
        parts.append(f"{human_size(freed)} freed locally")
    notify(APP_NAME, "Backup OK — " + ", ".join(parts) + ".", notify_enabled)
    return 0


def cmd_status(cfg: Config) -> int:
    """Print a summary of the backup state (reads only the manifest)."""
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
    """Report which files in a directory are safe to delete locally.

    Safe means byte-identical content exists in a chunk that is *confirmed
    uploaded* — once local chunks are deleted, Proton Drive is the only copy,
    so a pending chunk is not good enough. Advisory only; deletes nothing.
    """
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"Not a directory: {target}", file=sys.stderr)
        return 2
    manifest = Manifest.load(cfg.manifest_path)
    if not manifest.files:
        print("Nothing has been backed up yet — nothing is safe to delete.")
        return 1

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
    """Restore files by name/glob/folder, downloading only their chunks.

    A pattern ending in "/" restores exactly that folder subtree; otherwise
    globs and case-insensitive substrings match anywhere in the path. Every
    restored file is verified against its manifest SHA-256.
    """
    manifest = Manifest.load(cfg.manifest_path)
    if pattern.endswith("/"):
        matches = sorted(arc for arc in manifest.files if arc.startswith(pattern))
    else:
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


def cmd_dedupe(cfg: Config, min_mb: float, notify_enabled: bool) -> int:
    """Find files stored more than once and interactively keep one copy.

    Removed sync copies are re-verified and moved to the local Trash (the
    next backup run drops them from their chunks — removing only the backup
    entry would be useless, the mirror would re-add it). Removed archive
    copies are repacked out of their chunks.
    """
    manifest = Manifest.load(cfg.manifest_path)
    by_hash: dict[str, list[str]] = {}
    for arc, e in manifest.files.items():
        if e["size"] > 0:
            by_hash.setdefault(e["sha256"], []).append(arc)
    dupes = sorted(
        ((manifest.files[arcs[0]]["size"], sorted(arcs)) for arcs in by_hash.values()
         if len(arcs) > 1),
        key=lambda t: -t[0],
    )
    if not dupes:
        print("No duplicate files in the backup.")
        return 0

    threshold = int(min_mb * 1024**2)
    big = [(s, a) for s, a in dupes if s >= threshold]
    small = [(s, a) for s, a in dupes if s < threshold]
    waste = sum(s * (len(a) - 1) for s, a in dupes)
    print(f"{len(dupes)} duplicate group(s), {human_size(waste)} stored redundantly.")
    if small:
        small_waste = sum(s * (len(a) - 1) for s, a in small)
        print(f"Skipping {len(small)} group(s) under {min_mb:g} MB "
              f"({human_size(small_waste)}) — rerun with --dedupe 0 to include them.")
    if not big:
        return 0

    trash_local: list[str] = []
    remove_archive: set[str] = set()
    print("\nFor each group, choose the copy to KEEP. Removed sync copies are "
          "moved to the Trash locally;\nremoved archive copies are repacked "
          "out of their chunks.\n")
    for i, (size, arcs) in enumerate(big, 1):
        print(f"[{i}/{len(big)}] {human_size(size)} × {len(arcs)} identical copies:")
        for j, arc in enumerate(arcs, 1):
            e = manifest.files[arc]
            print(f"  {j}. {arc}  ({e['source']}, from {e['origin']})")
        try:
            choice = input("Keep which copy? [number / Enter=skip / q=stop] ").strip().lower()
        except EOFError:
            choice = "q"
        if choice == "q":
            break
        if not choice.isdigit() or not 1 <= int(choice) <= len(arcs):
            print("  skipped.\n")
            continue
        kept = arcs[int(choice) - 1]
        for arc in arcs:
            if arc == kept:
                continue
            if manifest.files[arc]["source"] == "sync":
                trash_local.append(arc)
            else:
                remove_archive.add(arc)
        print(f"  keeping {kept}\n")

    if not trash_local and not remove_archive:
        print("Nothing selected; no changes made.")
        return 0

    freed = 0
    for arc in trash_local:
        e = manifest.files[arc]
        p = Path(e["origin"])
        if not p.is_file():
            print(f"  already gone locally: {p}")
            continue
        st = p.stat()
        unchanged = (st.st_size == e["size"] and st.st_mtime_ns == e["mtime_ns"]) \
            or sha256_file(p) == e["sha256"]
        if not unchanged:
            print(f"  SKIPPED (file changed since backup): {p}")
            continue
        if move_to_trash(p):
            freed += e["size"]
            print(f"  trashed local copy: {p}")

    by_chunk: dict[str, set[str]] = {}
    for arc in remove_archive:
        by_chunk.setdefault(manifest.files[arc]["chunk"], set()).add(arc)
    for chunk_name, arcs in sorted(by_chunk.items()):
        try:
            print(f"  repacking {chunk_name} without {len(arcs)} duplicate(s)...")
            repack_archive_chunk(cfg, manifest, chunk_name, arcs)
        except UploadError as exc:
            print(f"  ERROR repacking {chunk_name}: {exc}", file=sys.stderr)
            print("  Stopping here; already-applied changes are saved.", file=sys.stderr)
            return 1

    print(f"\nDone. {human_size(freed)} moved to the Trash locally"
          f"{f', {len(remove_archive)} backup cop(ies) removed' if remove_archive else ''}.")
    try:
        answer = input("Run a backup now to rebuild chunks and upload the changes? [y/N] ")
    except EOFError:
        answer = "n"
    if answer.strip().lower() == "y":
        return cmd_backup(cfg, do_upload=True, dry_run=False, notify_enabled=notify_enabled)
    print("Chunks will be updated on the next backup run.")
    return 0


def cmd_init(config_path: Path) -> int:
    """Write the default config and create the data/dropzone directories."""
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
