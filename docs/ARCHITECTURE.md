# Architecture

## The chunk model

The backup is not one monolithic zip but a set of size-capped **chunks**
(default 500 MB), each an ordinary zip file, all living remotely under
`remote_folder` (e.g. `/Backups/MacBookAir/`) next to `manifest.json`.
Local chunk files are deleted as soon as their upload is confirmed — freeing
disk space is the point — so Proton Drive is the primary store, not a mirror.

Two kinds of chunk:

- **Sync chunks** (`sync-00001.zip`, …) hold files from the configured sync
  directories. When a member changes, the whole chunk is rebuilt *from the
  live local files* — sync data still exists on disk by definition, so an
  update never needs a download — and re-uploaded with `-c replace` under the
  same name. Chunks without a changed member are never touched: a one-file
  edit re-uploads one chunk, not the whole backup.
- **Archive chunks** (`arch-00002.zip`, …) hold dropzone files. They are
  write-once: once uploaded they are never rebuilt or re-uploaded, because
  their originals no longer exist locally.

### Files larger than the cap

A file bigger than `chunk_mb` is **never split across chunks** — it gets a
dedicated chunk containing just that file. Splitting would let one corrupted
chunk destroy the file and complicate restore for no benefit; Proton Drive
handles multi-GB single files fine. Additionally, already-compressed formats
(video, audio, photos, archives — see `STORED_SUFFIXES`) are stored in the
zip without recompression: deflating a 4 GB video costs minutes of CPU for
well under 1 % size gain.

## Pipeline of a backup run

```
lock (flock) ─▶ reconcile lost chunks ─▶ diff sync dirs ─▶ scan dropzone
  ─▶ plan (rebuild dirty sync chunks, pack new files into new chunks)
  ─▶ free-space check ─▶ build & verify chunks ─▶ save manifest
  ─▶ upload pending chunks ─▶ confirm remote ─▶ delete local chunk copies
  ─▶ upload manifest ─▶ trash dropzone originals (only now) ─▶ notify
```

Every run ends in exactly one macOS notification (unless `--no-notify`), and
a non-zero exit code on failure so launchd logs stay meaningful.

## Manifest (`manifest.json`)

```json
{
  "version": 2,
  "last_backup": "2026-07-04T21:00:00+02:00",
  "last_upload": "2026-07-04T21:02:11+02:00",
  "next_chunk": 7,
  "chunks": {
    "sync-00001.zip": {"kind": "sync", "size": 4812392, "sha256": "…",
                        "files": 213, "uploaded": "2026-07-04T21:01:40+02:00"},
    "arch-00002.zip": {"kind": "archive", "size": 91832771, "sha256": "…",
                        "files": 3, "uploaded": "2026-07-03T21:01:12+02:00"}
  },
  "files": {
    "Sync/Documents/notes.md": {
      "size": 1234, "mtime_ns": 1751648400000000000, "sha256": "…",
      "source": "sync", "origin": "/Users/you/Documents/notes.md",
      "chunk": "sync-00001.zip"
    }
  }
}
```

Archive layout inside chunks: sync dirs under `Sync/<dir-basename>/<relpath>`,
dropzone items under `Archive/<relpath>`. The manifest is written atomically
(temp file + `os.replace`) and re-saved after every uploaded chunk, so an
interrupted run resumes exactly where it stopped.

## I/O-efficient diffing

1. Walk each sync dir; for every file compare `(size, mtime_ns)` with the
   manifest. A match is trusted **without reading the file** — an unchanged
   tree costs one `stat()` per file and zero reads (rsync-style fast path).
2. Only new or stat-changed files are hashed, using `hashlib.file_digest`
   (chunked, implemented in C). A file that was touched but is byte-identical
   only gets its `mtime_ns` refreshed — no chunk rebuild.
3. The change set (`added`, `changed`, `deleted`, `touched`) maps to chunk
   work: changed/deleted members mark their chunk dirty for a rebuild; added
   files are first-fit packed into new chunks; a chunk whose last member was
   deleted is removed (and trashed remotely after the next successful sync).

## Upload lifecycle & deletion safety

The order of operations is the safety argument:

1. A chunk is built locally and verified: full CRC check, plus — for archive
   chunks — every member is streamed back out of the zip and its SHA-256
   compared to the original file's hash.
2. It is uploaded (`filesystem upload -c replace`), and its remote presence
   is checked with `filesystem info` (size comparison, best effort — the CLI
   offers no stronger integrity query).
3. Only then: the local chunk copy is deleted (unless `keep_local_chunks`),
   and — for archive chunks — the dropzone originals are moved to Finder's
   Trash, so even a catastrophic bug is recoverable until you empty it.

A failed or interrupted upload leaves everything in place: the chunk stays in
`backup_dir`, dropzone originals stay put, and the next run picks up the
pending uploads first. If a never-uploaded chunk vanishes from disk, sync
chunks are silently rebuilt from the live files and archive chunks are
dropped from the manifest — harmless, because their originals are still in
the dropzone and get re-archived.

Upload failures are classified by pattern-matching the CLI output — *storage
full*, *login expired*, *generic* — each with its own notification text (the
Proton CLI exposes no quota query, so detection is reactive).

## Restore

`--restore <name>` looks the file(s) up in the manifest, downloads **only the
chunk(s) that contain them** (or uses a local copy if one still exists),
extracts, and verifies each restored file against its manifest SHA-256.
Retrieving one file from a multi-hundred-GB backup costs one chunk download,
bounded by `chunk_mb` (or the file's own size, for oversized solo chunks).

## Concurrency & robustness

- A `flock` on `<backup_dir>/.lock` guarantees a manual run and the launchd
  job can never write chunks concurrently.
- Free-space check before building: the sum of all planned chunk sizes plus
  `min_free_gb` must fit.
- All subprocesses use explicit argument lists (no shell) and timeouts
  (6 h for transfers, 15 min otherwise); output is logged on failure.
- Logs rotate at 1 MB (`backup_organizer.log`, 3 backups kept).
