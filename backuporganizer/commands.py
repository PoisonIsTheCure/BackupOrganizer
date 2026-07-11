"""Top-level operations: backup, status, advice, restore, dedupe, init."""

from __future__ import annotations

import fnmatch
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .chunks import (build_chunk, check_free_space, pack_new_members,
                     reconcile_chunks, repack_archive_chunk)
from .config import DEFAULT_CONFIG, Config
from .manifest import Manifest
from .proton import (UPLOAD_FAIL_MESSAGES, UploadError, download_chunk, download_file,
                     list_remote_tree, proton, remote_list, upload_pending,
                     upload_sync_files)
from .scanner import (DROPZONE_SETTLE_SECONDS, diff_sync_dirs, find_sync_twins,
                      scan_dropzone, walk_files)
from .util import (APP_NAME, BackupError, human_size, log, move_to_trash, notify,
                   now_iso, sha256_file)


def cmd_backup(cfg: Config, do_upload: bool, dry_run: bool, notify_enabled: bool,
               settle_seconds: int = DROPZONE_SETTLE_SECONDS, json_out: bool = False) -> int:
    """One full backup run: diff, upload sync files, build archive chunks,
    upload, free local space.

    See docs/ARCHITECTURE.md for the pipeline and its safety ordering; the
    short version is that nothing local is ever trashed before the remote
    copy of its data is confirmed, and a synced file's cloud copy is never
    removed automatically — deleting it locally only marks it an orphan
    (see --orphans / delete-remote) for later manual removal.

    With json_out, prints one NDJSON progress event per line at each
    pipeline stage (for a GUI's live progress view), always ending in
    exactly one {"event": "result", ...} line summarizing the outcome —
    callers should key off that event, not "the last line", since a fatal
    error is reported the same way rather than as a Python exception.
    """
    def emit(event: dict) -> None:
        if json_out:
            print(json.dumps(event), flush=True)

    manifest = Manifest.load(cfg.manifest_path)
    reconcile_chunks(cfg, manifest)

    diff = diff_sync_dirs(cfg, manifest)
    dropzone_members, dropzone_groups = scan_dropzone(cfg, manifest, settle_seconds)
    new_arch_plans = pack_new_members(dropzone_members, cfg, manifest)

    log.info(
        "Diff: %d added, %d changed, %d deleted, %d touched, %d dropzone file(s) "
        "-> %d archive chunk(s).",
        len(diff.added), len(diff.changed), len(diff.deleted), len(diff.touched),
        len(dropzone_members), len(new_arch_plans),
    )
    emit({"event": "diff", "added": len(diff.added), "changed": len(diff.changed),
         "deleted": len(diff.deleted), "touched": len(diff.touched),
         "dropzone": len(dropzone_members)})

    if dry_run:
        for label, items in (
            ("SYNC ADD", [m.arcname for m in diff.added]),
            ("SYNC UPDATE", [m.arcname for m in diff.changed]),
            ("SYNC DELETE", diff.deleted),
            ("ARCHIVE", [m.arcname for m in dropzone_members]),
        ):
            for arc in items:
                print(f"{label:12} {arc}")
        for plan in new_arch_plans:
            print(f"CHUNK        {plan.name} ({len(plan.members)} file(s), "
                  f"{human_size(plan.total_bytes)})")
        print("\nNothing was modified.")
        return 0

    # ----- build archive chunks --------------------------------------------- #
    if new_arch_plans:
        try:
            check_free_space(cfg, sum(p.total_bytes for p in new_arch_plans))
            for i, plan in enumerate(new_arch_plans, 1):
                log.info("Building %s (%d file(s), %s)...",
                         plan.name, len(plan.members), human_size(plan.total_bytes))
                manifest.chunks[plan.name] = build_chunk(cfg, plan)
                emit({"event": "chunk_build", "name": plan.name, "index": i,
                     "total": len(new_arch_plans), "files": len(plan.members)})
        except BackupError as exc:
            if not json_out:
                raise  # let cli.py's top-level handler log + notify once
            log.error("%s", exc)
            notify(APP_NAME, f"Backup FAILED: {exc}", notify_enabled)
            emit({"event": "result", "ok": False, "error": str(exc)})
            return 1

    # ----- update manifest --------------------------------------------------- #
    for arc in diff.deleted:
        entry = manifest.files.pop(arc, None)
        if entry:
            manifest.deleted_sync[arc] = {
                "size": entry["size"], "sha256": entry["sha256"],
                "origin": entry["origin"], "deleted_at": now_iso(),
            }
    for m in diff.added:
        manifest.deleted_sync.pop(m.arcname, None)  # reappeared: no longer orphaned
        manifest.files[m.arcname] = m.to_entry_sync(uploaded="")
    for m in diff.changed:
        manifest.files[m.arcname] = m.to_entry_sync(uploaded="")
    for m in diff.touched:
        prior_uploaded = manifest.files[m.arcname].get("uploaded", "")
        manifest.files[m.arcname] = m.to_entry_sync(uploaded=prior_uploaded)
    for plan in new_arch_plans:
        for m in plan.members:
            manifest.files[m.arcname] = m.to_entry_archive(plan.name)
    manifest.last_backup = now_iso()
    manifest.save()

    # ----- upload sync files, then archive chunks, then free local space ---- #
    sync_uploaded = archive_uploaded = freed = 0
    upload_error: UploadError | None = None
    if do_upload:
        pending_sync = sum(
            1 for e in manifest.files.values()
            if e.get("source") == "sync" and not e.get("uploaded")
        )
        if pending_sync:
            emit({"event": "sync_upload_start", "count": pending_sync})
        sync_uploaded, upload_error = upload_sync_files(cfg, manifest, on_progress=emit)
        if upload_error:
            log.error("Sync upload failed (%s): %s", upload_error.kind, upload_error)

    if do_upload and upload_error is None:
        trash_remote = list(manifest.pending_remote_cleanup)
        pending_chunk_upload = any(
            not meta["uploaded"] and cfg.chunk_path(name).is_file()
            for name, meta in manifest.chunks.items()
        )
        if pending_chunk_upload or new_arch_plans or trash_remote:
            emit({"event": "archive_upload_start"})
            archive_uploaded, freed, upload_error = upload_pending(
                cfg, manifest, trash_remote, on_progress=emit)
            if upload_error:
                log.error("Archive upload failed (%s): %s", upload_error.kind, upload_error)
            else:
                manifest.pending_remote_cleanup = []
                manifest.save()

    # ----- trash dropzone originals whose chunks are confirmed uploaded ----- #
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
                emit({"event": "dropzone_trashed", "name": top.name})
        else:
            waiting += 1
            log.info("Dropzone entry kept until its upload is confirmed: %s", top.name)

    # ----- report ------------------------------------------------------------ #
    if upload_error:
        notify(APP_NAME, UPLOAD_FAIL_MESSAGES[upload_error.kind], notify_enabled)
        emit({"event": "result", "ok": False, "error": str(upload_error),
             "error_kind": upload_error.kind, "added": len(diff.added),
             "changed": len(diff.changed), "deleted": len(diff.deleted),
             "sync_uploaded": sync_uploaded, "archive_uploaded": archive_uploaded,
             "freed_bytes": freed, "trashed": trashed, "waiting": waiting})
        return 1
    parts = [f"{len(diff.added)} added, {len(diff.changed)} updated, {len(diff.deleted)} removed"]
    if trashed or waiting:
        parts.append(f"{trashed} dropzone item(s) offloaded")
    if sync_uploaded:
        parts.append(f"{sync_uploaded} sync file(s) uploaded")
    if archive_uploaded:
        parts.append(f"{archive_uploaded} chunk(s) uploaded")
    if freed:
        parts.append(f"{human_size(freed)} freed locally")
    notify(APP_NAME, "Backup OK — " + ", ".join(parts) + ".", notify_enabled)
    emit({"event": "result", "ok": True, "error": None, "added": len(diff.added),
         "changed": len(diff.changed), "deleted": len(diff.deleted),
         "sync_uploaded": sync_uploaded, "archive_uploaded": archive_uploaded,
         "freed_bytes": freed, "trashed": trashed, "waiting": waiting})
    return 0


def cmd_status(cfg: Config, json_out: bool = False) -> int:
    """Print a summary of the backup state (reads only the manifest)."""
    manifest = Manifest.load(cfg.manifest_path)
    total = len(manifest.files)
    total_bytes = sum(e["size"] for e in manifest.files.values())
    sync_n = sum(1 for e in manifest.files.values() if e.get("source") == "sync")
    arch_n = total - sync_n
    n_chunks = len(manifest.chunks)
    pending_chunks = [n for n, m in manifest.chunks.items() if not m["uploaded"]]
    pending_sync = sum(
        1 for e in manifest.files.values() if e.get("source") == "sync" and not e.get("uploaded")
    )
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

    if json_out:
        print(json.dumps({
            "files_total": total, "sync_count": sync_n, "archive_count": arch_n,
            "total_bytes": total_bytes, "chunks_total": n_chunks,
            "chunks_pending": sorted(pending_chunks), "sync_pending": pending_sync,
            "orphans_pending": len(manifest.deleted_sync),
            "local_cache_bytes": local_bytes, "last_backup": manifest.last_backup,
            "last_upload": manifest.last_upload, "dropzone_pending": pending,
            "remote_folder": cfg.remote_folder,
            "log_path": str(cfg.backup_dir / "backup_organizer.log"),
            "sync_dirs": [str(d) for d in cfg.sync_dirs],
        }))
        return 0

    lines = [
        f"{APP_NAME} status",
        "-" * 36,
        f"Files backed up:    {total}  ({sync_n} sync, {arch_n} archived)",
        f"Total size:         {human_size(total_bytes)}",
        f"Chunks:             {n_chunks} on {cfg.remote_folder}",
        f"Awaiting upload:    {len(pending_chunks)} chunk(s), {pending_sync} sync file(s)"
        + (f" ({', '.join(sorted(pending_chunks))})" if pending_chunks else ""),
        f"Local chunk cache:  {human_size(local_bytes)}",
        f"Last backup:        {manifest.last_backup or 'never'}",
        f"Last upload:        {manifest.last_upload or 'never'}",
        f"Dropzone pending:   {pending} item(s)",
        f"Orphaned in cloud:  {len(manifest.deleted_sync)} item(s) (see --orphans)",
    ]
    print("\n".join(lines))
    return 0


def cmd_log_tail(cfg: Config, n: int, json_out: bool = False) -> int:
    """Print the last n lines of backup_organizer.log — the rotating log
    every command writes INFO+ to (see util.setup_logging). This is the
    only place upload failures, unexpected exceptions, and every diff are
    recorded; --status/--list only ever show current state, not history.
    """
    log_path = cfg.backup_dir / "backup_organizer.log"
    if not log_path.is_file():
        if json_out:
            print(json.dumps({"path": str(log_path), "lines": []}))
        else:
            print(f"No log file yet: {log_path}")
        return 1
    all_lines = log_path.read_text(errors="replace").splitlines()
    tail = all_lines[-n:] if n > 0 else all_lines
    if json_out:
        print(json.dumps({"path": str(log_path), "lines": tail}))
        return 0
    print(f"# {log_path} (last {len(tail)} of {len(all_lines)} lines)")
    for line in tail:
        print(line)
    return 0


def cmd_list(cfg: Config, kind: str, json_out: bool = False) -> int:
    """List backed-up files (kind: "sync", "archive", or "all")."""
    manifest = Manifest.load(cfg.manifest_path)
    entries = []
    for arc, e in sorted(manifest.files.items()):
        source = e.get("source")
        if kind == "sync" and source != "sync":
            continue
        if kind == "archive" and source != "dropzone":
            continue
        # "uploaded" is always an ISO timestamp string when uploaded, "" when
        # pending — same type for sync and archive rows, so callers (the
        # Swift JSON decoder) don't need a union type for one field.
        row = {"arcname": arc, "size": e["size"], "sha256": e["sha256"], "source": source}
        if source == "sync":
            row["uploaded"] = e.get("uploaded", "")
        else:
            row["chunk"] = e.get("chunk", "")
            chunk_uploaded = manifest.chunks.get(e.get("chunk", ""), {}).get("uploaded", "")
            row["uploaded"] = chunk_uploaded
            twins = find_sync_twins(manifest, arc)
            row["relocatable"] = bool(twins)
            row["twin_confirmed"] = bool(chunk_uploaded)
        entries.append(row)
    if json_out:
        print(json.dumps(entries))
        return 0
    if not entries:
        print(f"Nothing backed up ({kind}).")
        return 1
    for row in entries:
        print(f"{row['arcname']}  ({human_size(row['size'])}, {row['source']})")
    return 0


def cmd_advice(cfg: Config, target: Path) -> int:
    """Report which files in a directory are safe to delete locally.

    Safe means byte-identical content is *confirmed uploaded* — for an
    archived file that means its chunk is confirmed uploaded, for a synced
    file its own per-file upload is confirmed. Advisory only; deletes
    nothing (and for a synced file, "safe" here is about local disk space
    only — the cloud copy stays until manually deleted; see --orphans).
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
        if entry.get("source") == "dropzone":
            return bool(manifest.chunks.get(entry.get("chunk", ""), {}).get("uploaded"))
        return bool(entry.get("uploaded"))

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
    """Restore files by name/glob/folder.

    Archived files download only their chunk; synced files download
    individually (they were never zipped). A pattern ending in "/" restores
    exactly that folder subtree; otherwise globs and case-insensitive
    substrings match anywhere in the path. Every restored file is verified
    against its manifest SHA-256.
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

    sync_arcs = [a for a in matches if manifest.files[a].get("source") == "sync"]
    by_chunk: dict[str, list[str]] = {}
    for arc in matches:
        if arc in sync_arcs:
            continue
        by_chunk.setdefault(manifest.files[arc]["chunk"], []).append(arc)
    print(f"Restoring {len(matches)} file(s) "
          f"({len(sync_arcs)} synced, {len(by_chunk)} archive chunk(s)) to {dest}")

    failures = 0
    with tempfile.TemporaryDirectory(dir=cfg.backup_dir if cfg.backup_dir.is_dir() else None) as tmp:
        for arc in sync_arcs:
            out = dest / arc
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = download_file(cfg, cfg.remote_path(arc), Path(tmp))
                shutil.move(str(downloaded), out)
            except UploadError as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                failures += 1
                continue
            if sha256_file(out) == manifest.files[arc]["sha256"]:
                print(f"  OK  {arc}")
            else:
                failures += 1
                print(f"  HASH MISMATCH  {arc} (restored file kept for inspection)",
                      file=sys.stderr)
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


def cmd_orphans(cfg: Config, json_out: bool = False) -> int:
    """List synced files deleted locally but still present in the cloud —
    read-only, deletes nothing. See delete-remote to actually remove one."""
    manifest = Manifest.load(cfg.manifest_path)
    orphans = [
        {"arcname": arc, **entry} for arc, entry in sorted(manifest.deleted_sync.items())
    ]
    if json_out:
        print(json.dumps(orphans))
        return 0
    if not orphans:
        print("No orphaned cloud copies.")
        return 0
    print(f"{len(orphans)} synced file(s) removed locally, still in the cloud:")
    for o in orphans:
        print(f"  {o['arcname']}  ({human_size(o['size'])}, deleted locally {o['deleted_at']})")
    print("\nRun `delete-remote ARCNAME...` to permanently remove a cloud copy.")
    return 0


def cmd_delete_remote(cfg: Config, arcnames: list[str], json_out: bool = False) -> int:
    """Permanently delete the cloud copy of an orphaned synced file.

    Scoped to orphans only (manifest.deleted_sync): an arcname still live in
    manifest.files is refused, since deleting its cloud copy would just get
    silently re-uploaded on the next run — removing a file from sync
    entirely is a sync_dirs config edit, not a per-file action.
    """
    manifest = Manifest.load(cfg.manifest_path)
    deleted: list[str] = []
    not_found: list[str] = []
    errors: dict[str, str] = {}
    for arc in arcnames:
        if arc not in manifest.deleted_sync:
            not_found.append(arc)
            continue
        proc = proton(cfg, "filesystem", "trash", cfg.remote_path(arc))
        if proc.returncode != 0:
            errors[arc] = (proc.stderr + proc.stdout).strip()[:500]
            continue
        del manifest.deleted_sync[arc]
        deleted.append(arc)
    manifest.save()
    result = {"deleted": deleted, "not_found": not_found, "errors": errors}
    if json_out:
        print(json.dumps(result))
    else:
        for arc in deleted:
            print(f"Deleted from the cloud: {arc}")
        for arc in not_found:
            print(f"Not an orphaned synced file, skipped: {arc}", file=sys.stderr)
        for arc, msg in errors.items():
            print(f"ERROR deleting {arc}: {msg}", file=sys.stderr)
    return 1 if (not_found or errors) else 0


def cmd_retire_sync_twin(cfg: Config, archive_arcnames: list[str],
                         json_out: bool = False) -> int:
    """Retire the synced copy(ies) of already-archived file(s): trash the
    remote sync copy and move the local sync-dir original to the Trash,
    keeping only the archived (zipped) copy.

    Does not prompt — the caller (the GUI, after its own confirmation
    dialog) must already have decided to do this. Still refuses to act on
    an unsafe state: the archive copy's chunk must be confirmed uploaded
    before its sync twin is touched, and each twin is re-verified
    byte-identical immediately before it's trashed (it may have changed
    since the archive copy was made).
    """
    manifest = Manifest.load(cfg.manifest_path)
    retired: list[dict] = []
    skipped: list[dict] = []
    for arc in archive_arcnames:
        entry = manifest.files.get(arc)
        if not entry or entry.get("source") != "dropzone":
            skipped.append({"archive_arcname": arc, "reason": "not an archived file"})
            continue
        if not manifest.chunks.get(entry.get("chunk", ""), {}).get("uploaded"):
            skipped.append({"archive_arcname": arc, "reason": "archive copy not confirmed uploaded"})
            continue
        twins = find_sync_twins(manifest, arc)
        if not twins:
            skipped.append({"archive_arcname": arc, "reason": "no live sync twin"})
            continue
        for sync_arc, twin in twins:
            p = Path(twin["origin"])
            if not p.is_file():
                skipped.append({"archive_arcname": arc, "sync_arcname": sync_arc,
                                "reason": "sync original already gone"})
                continue
            st = p.stat()
            unchanged = (st.st_size == twin["size"] and st.st_mtime_ns == twin["mtime_ns"]) \
                or sha256_file(p) == twin["sha256"]
            if not unchanged:
                skipped.append({"archive_arcname": arc, "sync_arcname": sync_arc,
                                "reason": "sync copy changed since archiving"})
                continue
            if twin.get("uploaded"):
                proc = proton(cfg, "filesystem", "trash", cfg.remote_path(sync_arc))
                if proc.returncode != 0:
                    skipped.append({"archive_arcname": arc, "sync_arcname": sync_arc,
                                    "reason": f"remote trash failed: {proc.stderr.strip()[:200]}"})
                    continue
            if not move_to_trash(p):
                skipped.append({"archive_arcname": arc, "sync_arcname": sync_arc,
                                "reason": "local trash failed"})
                continue
            manifest.files.pop(sync_arc, None)
            retired.append({"archive_arcname": arc, "sync_arcname": sync_arc})
    manifest.save()
    result = {"retired": retired, "skipped": skipped}
    if json_out:
        print(json.dumps(result))
    else:
        for r in retired:
            print(f"Retired synced copy of {r['archive_arcname']}: {r['sync_arcname']}")
        for s in skipped:
            print(f"Skipped {s['archive_arcname']}: {s['reason']}", file=sys.stderr)
    return 1 if skipped and not retired else 0


def cmd_recalculate_manifest(cfg: Config, json_out: bool = False) -> int:
    """Reconcile the local manifest against what's actually on Proton Drive
    — the cloud is treated as ground truth for "is this file really up
    there," recovering from a lost/stale/wrong local manifest.

    Sync side (deep): lists the whole remote Sync/ tree and cross-checks
    every entry against local files — no downloads. A remote file with a
    matching local original (by mapping its arcname's directory segment
    back to a configured sync_dir) is fully recovered (hashed locally,
    marked uploaded). A remote file with no local match is only reported,
    not downloaded — bandwidth for the whole tree isn't spent by surprise;
    see --restore to fetch a specific one if you want it back. A local
    entry claiming "uploaded" that isn't actually on the remote is reset to
    pending (the next `run` re-uploads it). Orphans no longer present on
    the remote are dropped (nothing left to delete-remote).

    Archive side (shallow): only confirms each chunk in manifest.chunks
    still exists remotely at the right size — does not attempt to recover
    a chunk's internal file list from scratch (that needs downloading and
    opening the zip, out of scope here).
    """
    manifest = Manifest.load(cfg.manifest_path)

    # ----- sync: deep reconciliation ---------------------------------------- #
    remote_sync = list_remote_tree(cfg, cfg.remote_path("Sync"))
    base_len = len(cfg.remote_folder.rstrip("/")) + 1
    remote_by_arc = {path[base_len:]: meta for path, meta in remote_sync.items()}
    sync_dir_by_basename = {d.name: d for d in cfg.sync_dirs}

    newly_recovered: list[str] = []
    confirmed_pending: list[str] = []
    size_mismatch: list[str] = []
    unmatched_no_local: list[str] = []

    def recover_entry(arc: str, origin: Path) -> None:
        st = origin.stat()
        manifest.files[arc] = {
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha256_file(origin),
            "source": "sync", "origin": str(origin), "uploaded": now_iso(),
        }

    for arc, meta in remote_by_arc.items():
        parts = arc.split("/")
        if len(parts) < 3 or parts[0] != "Sync":
            continue  # not a mirrored sync path we recognize
        entry = manifest.files.get(arc)
        if entry is not None and entry.get("source") == "sync":
            if not entry.get("uploaded"):
                origin = Path(entry["origin"])
                if origin.is_file():
                    recover_entry(arc, origin)
                    confirmed_pending.append(arc)
                else:
                    unmatched_no_local.append(arc)
            elif entry["size"] != meta["size"]:
                size_mismatch.append(arc)
                origin = Path(entry["origin"])
                if origin.is_file():
                    recover_entry(arc, origin)
            continue
        sync_dir = sync_dir_by_basename.get(parts[1])
        origin = (sync_dir / "/".join(parts[2:])) if sync_dir else None
        if origin is not None and origin.is_file():
            recover_entry(arc, origin)
            manifest.deleted_sync.pop(arc, None)
            newly_recovered.append(arc)
        else:
            unmatched_no_local.append(arc)

    stale_cleared = []
    for arc, entry in manifest.files.items():
        if entry.get("source") == "sync" and entry.get("uploaded") and arc not in remote_by_arc:
            entry["uploaded"] = ""
            stale_cleared.append(arc)

    orphans_cleared = []
    for arc in list(manifest.deleted_sync):
        if arc not in remote_by_arc:
            del manifest.deleted_sync[arc]
            orphans_cleared.append(arc)

    # ----- archive: shallow presence/size check ------------------------------ #
    remote_top = {item["name"]: item["size"] for item in remote_list(cfg, cfg.remote_folder)
                  if item["type"] != "folder"}
    archive_confirmed: list[str] = []
    archive_reset: list[str] = []
    for name, meta in manifest.chunks.items():
        if not meta.get("uploaded"):
            continue
        if remote_top.get(name) == meta["size"]:
            archive_confirmed.append(name)
        else:
            meta["uploaded"] = ""
            archive_reset.append(name)

    manifest.save()
    result = {
        "sync": {
            "remote_sync_files": len(remote_by_arc),
            "newly_recovered": newly_recovered, "confirmed_pending": confirmed_pending,
            "size_mismatch": size_mismatch, "unmatched_no_local": unmatched_no_local,
            "stale_cleared": stale_cleared, "orphans_cleared": orphans_cleared,
        },
        "archive": {"confirmed": archive_confirmed, "reset_to_pending": archive_reset},
    }
    if json_out:
        print(json.dumps(result))
    else:
        s = result["sync"]
        print(f"Sync: {s['remote_sync_files']} file(s) on the cloud — "
              f"{len(s['newly_recovered'])} recovered, {len(s['confirmed_pending'])} confirmed, "
              f"{len(s['size_mismatch'])} size mismatch, {len(s['unmatched_no_local'])} unmatched "
              f"(no local original), {len(s['stale_cleared'])} reset to pending, "
              f"{len(s['orphans_cleared'])} stale orphan(s) cleared.")
        a = result["archive"]
        print(f"Archive: {len(a['confirmed'])} chunk(s) confirmed, "
              f"{len(a['reset_to_pending'])} reset to pending.")
    return 0


def cmd_dedupe(cfg: Config, min_mb: float, notify_enabled: bool) -> int:
    """Find files stored more than once and interactively keep one copy.

    Removed sync copies are re-verified and moved to the local Trash; the
    next backup run notices they're gone and marks their cloud copies as
    orphaned for later manual removal (see --orphans / delete-remote).
    Removed archive copies are repacked out of their chunks.
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


def cmd_archive(cfg: Config, paths: list[Path], run: bool, do_upload: bool,
                notify_enabled: bool, json_out: bool = False) -> int:
    """Move files/folders into the dropzone, then run the full cycle.

    The dropzone settle delay is skipped: an explicit `archive` invocation
    means the files are complete. Archiving something out of a sync dir just
    moves the file — the sync side sees the deletion (and the cloud sync
    copy becomes an orphan, kept until manually deleted; see --orphans),
    while the archive side stores the new zipped copy.

    With json_out, prints one JSON object instead of the human-readable
    log, including a "relocatable" list of archived files that still have a
    live sync-dir twin with identical content — callers (the GUI) use this
    to offer retiring that twin via retire-sync-twin.
    """
    cfg.dropzone.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []  # (original, dest)
    skipped: list[str] = []
    for raw in paths:
        p = raw.expanduser()
        if not p.exists():
            skipped.append(str(p))
            if not json_out:
                print(f"Not found, skipped: {p}", file=sys.stderr)
            continue
        if cfg.dropzone in p.parents or p == cfg.dropzone:
            if not json_out:
                print(f"Already in the dropzone, skipped: {p}")
            continue
        dest = cfg.dropzone / p.name
        if dest.exists():
            stamp = now_iso().replace(":", "").replace("-", "")[:15]
            dest = cfg.dropzone / f"{p.stem}_{stamp}{p.suffix}"
        shutil.move(str(p), dest)
        moved.append((p, dest))
        if not json_out:
            print(f"→ dropzone: {p}  (as {dest.name})")
    if not moved:
        if json_out:
            print(json.dumps({"moved": [], "skipped": skipped, "relocatable": []}))
        else:
            print("Nothing to archive.")
        return 1
    if not run:
        if json_out:
            print(json.dumps({
                "moved": [str(d) for _, d in moved], "skipped": skipped,
                "staged_only": True, "relocatable": [],
            }))
        else:
            print(f"{len(moved)} item(s) staged; they will be archived on the next backup run.")
        return 0
    if not json_out:
        print(f"{len(moved)} item(s) staged — running the backup cycle now.")
    rc = cmd_backup(cfg, do_upload=do_upload, dry_run=False,
                    notify_enabled=notify_enabled, settle_seconds=0)
    if json_out:
        manifest = Manifest.load(cfg.manifest_path)
        dest_roots = [d for _, d in moved]
        relocatable = []
        for arc, e in manifest.files.items():
            if e.get("source") != "dropzone":
                continue
            origin = Path(e["origin"])
            if not any(origin == root or root in origin.parents for root in dest_roots):
                continue
            for sync_arc, _ in find_sync_twins(manifest, arc):
                relocatable.append({
                    "archive_arcname": arc, "sync_arcname": sync_arc,
                    "twin_confirmed": bool(manifest.chunks.get(e.get("chunk", ""), {}).get("uploaded")),
                })
        print(json.dumps({
            "moved": [str(d) for _, d in moved], "skipped": skipped,
            "staged_only": False, "backup_exit_code": rc, "relocatable": relocatable,
        }))
    return rc


def cmd_add_sync(config_path: Path, dirs: list[Path], run: bool, do_upload: bool,
                 notify_enabled: bool, json_out: bool = False) -> int:
    """Add folder(s) to sync_dirs in the config, then run the full cycle.

    The updated config is validated (existence, basename collisions,
    dropzone overlap) before the file is rewritten; on any error nothing
    is changed. Single files cannot be synced — archive them instead.
    """
    raw = json.loads(config_path.read_text())
    current = [str(Path(d).expanduser()) for d in raw.get("sync_dirs", [])]
    added = []
    skipped = []
    for d in dirs:
        p = d.expanduser().resolve()
        if not p.is_dir():
            if json_out:
                print(json.dumps({"error": f"Not a directory: {p}"}))
            else:
                print(f"Not a directory: {p} — sync entries must be folders; "
                      "use `archive` for single files.", file=sys.stderr)
            return 2
        if str(p) in current:
            skipped.append(str(p))
            if not json_out:
                print(f"Already a sync dir, skipped: {p}")
            continue
        current.append(str(p))
        added.append(p)
    if not added:
        if json_out:
            print(json.dumps({"added": [], "skipped": skipped}))
        else:
            print("Nothing new to add.")
        return 1

    candidate = dict(raw)
    candidate["sync_dirs"] = current
    cfg = Config.from_raw(candidate)  # raises ConfigError on collisions/overlap

    config_path.write_text(json.dumps(candidate, indent=2) + "\n")
    if not json_out:
        for p in added:
            print(f"Added to sync: {p}")
    if not run:
        if json_out:
            print(json.dumps({"added": [str(p) for p in added], "skipped": skipped,
                              "staged_only": True}))
        else:
            print("They will be backed up on the next run.")
        return 0
    if not json_out:
        print("Running the backup cycle now.")
    rc = cmd_backup(cfg, do_upload=do_upload, dry_run=False, notify_enabled=notify_enabled)
    if json_out:
        print(json.dumps({"added": [str(p) for p in added], "skipped": skipped,
                          "staged_only": False, "backup_exit_code": rc}))
    return rc


def cmd_remove_sync(config_path: Path, dirs: list[Path], json_out: bool = False) -> int:
    """Stop syncing folder(s): removes them from sync_dirs and orphans every
    currently-tracked cloud copy under them — exactly as if each file had
    been deleted locally, since nothing here is watched for changes from
    this point on. Cloud copies are kept until a manual delete-remote, same
    as any other synced-file deletion; nothing is uploaded, downloaded, or
    deleted here.
    """
    raw = json.loads(config_path.read_text())
    current = [str(Path(d).expanduser()) for d in raw.get("sync_dirs", [])]
    to_remove: list[str] = []
    not_found: list[str] = []
    for d in dirs:
        p = str(d.expanduser())
        (to_remove if p in current else not_found).append(p)
    if not to_remove:
        result = {"removed": [], "not_found": not_found, "orphaned": []}
        if json_out:
            print(json.dumps(result))
        else:
            print("Not a configured sync folder, nothing removed.", file=sys.stderr)
        return 1

    remaining = [d for d in current if d not in to_remove]
    candidate = dict(raw)
    candidate["sync_dirs"] = remaining
    cfg = Config.from_raw(candidate)  # validated even though a removal can't reintroduce collisions

    manifest = Manifest.load(cfg.manifest_path)
    removed_basenames = {Path(p).name for p in to_remove}
    orphaned: list[str] = []
    for arc, entry in list(manifest.files.items()):
        if entry.get("source") != "sync":
            continue
        parts = arc.split("/", 2)
        if len(parts) < 2 or parts[0] != "Sync" or parts[1] not in removed_basenames:
            continue
        del manifest.files[arc]
        manifest.deleted_sync[arc] = {
            "size": entry["size"], "sha256": entry["sha256"],
            "origin": entry["origin"], "deleted_at": now_iso(),
        }
        orphaned.append(arc)
    manifest.save()

    config_path.write_text(json.dumps(candidate, indent=2) + "\n")
    result = {"removed": to_remove, "not_found": not_found, "orphaned": orphaned}
    if json_out:
        print(json.dumps(result))
    else:
        for p in to_remove:
            print(f"Stopped syncing: {p}")
        if orphaned:
            print(f"{len(orphaned)} cloud cop(ies) kept as orphans — see --orphans / delete-remote.")
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
