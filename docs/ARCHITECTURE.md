# Architecture

## Pipeline of a backup run

```
lock (flock) ─▶ diff sync dirs ─▶ scan dropzone ─▶ free-space check
      ─▶ write zip (create or in-place update) ─▶ verify (CRC + SHA-256)
      ─▶ rotate zip filename ─▶ save manifest ─▶ trash verified dropzone items
      ─▶ upload to Proton Drive ─▶ prune old remote zips ─▶ notify
```

Every run ends in exactly one macOS notification (unless `--no-notify`), and a
non-zero exit code on failure so launchd logs stay meaningful.

## Manifest (`manifest.json`)

```json
{
  "version": 1,
  "zip_name": "backup_20260704_210000.zip",
  "last_backup": "2026-07-04T21:00:00+04:00",
  "last_upload": "2026-07-04T21:02:11+04:00",
  "files": {
    "Sync/Documents/notes.md": {
      "size": 1234,
      "mtime_ns": 1751648400000000000,
      "sha256": "…",
      "source": "sync",
      "origin": "/Users/you/Documents/notes.md"
    }
  }
}
```

Archive layout: sync dirs live under `Sync/<dir-basename>/<relative-path>`,
dropzone items under `Archive/<relative-path>`. The manifest is written
atomically (temp file + `os.replace`), so a crash can never leave a truncated
manifest next to a good archive.

## I/O-efficient diffing

1. Walk each sync dir; for every file compare `(size, mtime_ns)` with the
   manifest. A match is trusted **without reading the file** — an unchanged
   tree costs one `stat()` per file and zero reads (rsync-style fast path).
2. Only new or stat-changed files are hashed, using `hashlib.file_digest`
   (chunked, implemented in C). A file that was touched but is byte-identical
   only gets its `mtime_ns` refreshed in the manifest — no archive write.
3. The result is a classified change set: `added`, `changed`, `deleted`,
   `touched`.

## Zip update strategy

`zipfile.ZipFile` cannot replace or remove entries in place, so:

- **First build / recovery** — a fresh archive is written with `zipfile`
  (to a temp name, then atomically renamed).
- **Updates** — added/changed files are staged as *symlinks* in a temp tree
  mirroring the archive layout, and the system `/usr/bin/zip` is invoked from
  that tree. Plain add mode replaces existing entries unconditionally (unlike
  `zip -u`, which trusts mtimes) and follows symlinks, so no data is copied
  during staging and only the changed entries are recompressed. Deletions go
  through `zip -d` (with glob metacharacters escaped, since `-d` takes
  patterns). `zip` rewrites the archive container, but never re-reads or
  recompresses unchanged entries' data.

After every write the archive is CRC-checked (`ZipFile.testzip()`).

## Dropzone verification & deletion safety

A dropzone original is moved to the Trash only after all of these hold:

1. its SHA-256 was computed from the local file,
2. the copy **inside the finished zip** was streamed back out and re-hashed,
3. the two hashes match, and
4. (for folders) every file in the folder passed 1–3.

Deletion uses Finder's Trash via `osascript`, so even a catastrophic bug is
recoverable until you empty the Trash. If verification fails, the file stays,
the manifest does not record it, and the run reports failure — the next run
retries automatically.

If the archive itself disappears from disk, the tool rebuilds it from the sync
dirs, but archived dropzone entries cannot be restored locally; they are
dropped from the manifest and listed loudly in the log and a notification, so
the advisor never calls a file "safe to delete" against a backup that no
longer exists.

## Storage safety checks

- **Local, before writing**: `shutil.disk_usage` must show room for
  *current archive size + bytes being added + `min_free_gb`* — the worst case,
  because `zip` builds a temp copy of the archive next to the original.
- **Before upload**: optional `max_zip_gb` cap.
- **Remote**: the Proton Drive CLI has no quota query, so upload failures are
  classified by pattern-matching the CLI output into *storage full*, *login
  expired* and *generic* — each with its own notification text.

## Upload & remote retention

The archive filename is timestamped, so each upload creates a new remote file
(`manifest.json` is overwritten in place via `-c replace`). Remote zips beyond
`remote_keep` are moved to Proton's trash **only after** the new upload
succeeded — the previous good backup is never deleted first. A failed upload
leaves the local backup intact and is retried on the next run.

## Concurrency & robustness

- A `flock` on `<backup_dir>/.lock` guarantees a manual run and the launchd
  job can never write the archive concurrently.
- All subprocesses use explicit argument lists (no shell), timeouts, and their
  output is logged on failure.
- Logs rotate at 1 MB (`backup_organizer.log`, 3 backups kept).
