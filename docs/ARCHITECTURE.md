# Architecture

## Two storage models

Backed-up content splits into two kinds with genuinely different remote
representations:

- **Synced** files (from `sync_dirs`) are uploaded as **plain files**,
  mirroring the local directory shape remotely under `Sync/<dir-basename>/…`
  — no zipping, nothing written to local disk beyond the manifest. Deleting
  one locally never deletes its cloud copy automatically (see "Orphans and
  manual deletion" below).
- **Archived** files (from the dropzone) are packed into size-capped
  **chunks** (default 500 MB), each an ordinary zip file, under
  `arch-00001.zip`, … They are write-once: once uploaded they are never
  rebuilt, because their dropzone originals no longer exist locally (moved to
  the Trash only after a confirmed upload).

Both live remotely under `remote_folder` (e.g. `/my-files/Backups/MacBookAir/`)
next to `manifest.json`.

### Files larger than the chunk cap

An archive file bigger than `chunk_mb` is **never split across chunks** — it
gets a dedicated chunk containing just that file. Splitting would let one
corrupted chunk destroy the file and complicate restore for no benefit;
Proton Drive handles multi-GB single files fine. Already-compressed formats
(video, audio, photos, archives — see `STORED_SUFFIXES`) are stored in the
zip without recompression: deflating a 4 GB video costs minutes of CPU for
well under 1% size gain. Sync files have no cap at all — each uploads as
itself, however large.

## Pipeline of a backup run

```
lock (flock) ─▶ reconcile lost archive chunks ─▶ diff sync dirs ─▶ scan dropzone
  ─▶ pack new dropzone files into archive chunks ─▶ free-space check
  ─▶ build & verify archive chunks ─▶ update manifest (incl. orphaning sync
     deletions) ─▶ save manifest
  ─▶ upload sync files (per-file, mirrored remote dirs) ─▶ confirm remote
  ─▶ upload pending archive chunks ─▶ confirm remote ─▶ delete local chunk
     copies ─▶ upload manifest ─▶ trash dropzone originals (only now) ─▶ notify
```

Every run ends in exactly one macOS notification (unless `--no-notify`), and
a non-zero exit code on failure so launchd logs stay meaningful.

## Manifest (`manifest.json`, version 3)

```json
{
  "version": 3,
  "last_backup": "2026-07-11T21:00:00+02:00",
  "last_upload": "2026-07-11T21:02:11+02:00",
  "next_chunk": 7,
  "remote_dirs": ["/my-files/Backups/MacBookAir/Sync/Documents"],
  "pending_remote_cleanup": [],
  "chunks": {
    "arch-00002.zip": {"kind": "archive", "size": 91832771, "sha256": "…",
                        "files": 3, "uploaded": "2026-07-03T21:01:12+02:00"}
  },
  "files": {
    "Sync/Documents/notes.md": {
      "size": 1234, "mtime_ns": 1751648400000000000, "sha256": "…",
      "source": "sync", "origin": "/Users/you/Documents/notes.md",
      "uploaded": "2026-07-11T21:00:00+02:00"
    },
    "Archive/Photos/vacation.jpg": {
      "size": 812233, "mtime_ns": 1751648400000000000, "sha256": "…",
      "source": "dropzone", "origin": "/Users/you/Backups/ArchiveDropzone/Photos/vacation.jpg",
      "chunk": "arch-00002.zip"
    }
  },
  "deleted_sync": {
    "Sync/Documents/old_notes.md": {
      "size": 4096, "sha256": "…", "origin": "/Users/you/Documents/old_notes.md",
      "deleted_at": "2026-07-10T09:00:00+02:00"
    }
  }
}
```

Sync file entries carry their own `uploaded` timestamp and no `chunk` key;
archive file entries are unchanged from before (`chunk` names their zip, no
per-file `uploaded`). `chunks` only ever holds archive entries now.
`remote_dirs` caches which remote folders already exist, so a largely static
sync tree doesn't re-issue `create-folder` calls every run. `deleted_sync`
holds *orphans*: synced files removed locally whose cloud copy is
deliberately kept until a manual `delete-remote`. `pending_remote_cleanup`
only exists transiently, right after a v2→v3 migration (see below).

The manifest is written atomically (temp file + `os.replace`) and re-saved
after every uploaded file/chunk, so an interrupted run resumes exactly where
it stopped.

### Migrating from v2

A v2 manifest (the old all-chunked format, where sync files were also zipped
into `sync-*.zip` chunks) is backed up untouched to `manifest.v2.bak.json`,
then migrated in place: sync file entries and sync chunks are stripped out,
and any of those old chunks that were confirmed uploaded are queued in
`pending_remote_cleanup` so the next successful upload trashes them
remotely. Every sync file then re-uploads fresh as a plain mirrored file —
this is safe and cheap because sync-dir originals are never deleted by this
tool, so every file's `origin` is still on disk; there's no reason to
download-then-reupload the old zips first. Archive data is untouched by the
migration.

## I/O-efficient diffing

1. Walk each sync dir; for every file compare `(size, mtime_ns)` with the
   manifest. A match is trusted **without reading the file** — an unchanged
   tree costs one `stat()` per file and zero reads (rsync-style fast path).
2. Only new or stat-changed files are hashed, using `hashlib.file_digest`
   (chunked, implemented in C). A file that was touched but is byte-identical
   only gets its `mtime_ns` refreshed — no re-upload.
3. The change set (`added`, `changed`, `deleted`, `touched`) maps directly to
   work: added/changed files upload (or re-upload) individually; a deleted
   file's manifest entry moves into `deleted_sync` as an orphan, never
   trashed remotely on its own.

## Orphans and manual deletion

Deleting a synced file locally is *never* propagated to the cloud
automatically — the next backup run notices the file is gone locally and
moves its manifest entry into `deleted_sync` (an orphan): still on Proton
Drive, no longer tracked as a live local file. `--orphans` lists them;
`delete-remote ARCNAME` is the only thing that ever removes an orphan's
cloud copy (refuses to act on anything not actually orphaned, so it can't be
used to accidentally nuke a still-synced file's cloud copy). If a file
reappears at the same local path, it's simply treated as newly added again
and its orphan record is cleared.

## Retiring a synced copy after archiving

Archiving something out of a sync dir (`archive PATH`) moves it to the
dropzone and zips it as usual — the sync side just sees a deletion (orphan,
as above) and the archive side gets a new zipped copy; both can coexist.
`retire-sync-twin ARCHIVE_ARCNAME` is the explicit, non-prompting action
that collapses this down to one copy: it requires the archive copy's chunk
to already be confirmed uploaded, re-verifies each same-content sync entry
(`find_sync_twins`) is still byte-identical, then trashes its remote copy
and moves the local original to the Trash. It never prompts itself — the
caller (the GUI, after its own confirmation dialog) has already decided.

## Upload lifecycle & deletion safety

The order of operations is the safety argument:

1. An archive chunk is built locally and verified: full CRC check, plus
   every member is streamed back out of the zip and its SHA-256 compared to
   the original file's hash. A sync file has no local build step — nothing
   to verify before upload beyond its own hash.
2. Each is uploaded (`filesystem upload -c replace`), and its remote
   presence is checked with `filesystem info` (size + SHA-1 comparison, best
   effort — the CLI offers no stronger integrity query). Sync files are
   batch-uploaded per remote directory but confirmed one at a time (`info`
   takes exactly one path).
3. Only then: the local archive chunk copy is deleted (unless
   `keep_local_chunks`), and the dropzone originals are moved to Finder's
   Trash, so even a catastrophic bug is recoverable until you empty it. A
   synced file's local original is *never* deleted by any of this — sync is
   a live mirror, not archive-then-free-space.

A failed or interrupted upload leaves everything in place: any built chunk
stays in `backup_dir`, dropzone originals stay put, unsent sync files stay
marked pending (`uploaded: ""`), and the next run picks up where it left
off. If a never-uploaded archive chunk vanishes from disk, it's dropped from
the manifest — harmless, because its originals are still in the dropzone and
get re-archived.

Upload failures are classified by pattern-matching the CLI output — *storage
full*, *login expired*, *generic* — each with its own notification text (the
Proton CLI exposes no quota query, so detection is reactive).

## Restore

`--restore <name>` looks the file(s) up in the manifest. Synced files
download individually (they were never zipped); archived files download
only the chunk(s) that contain them (or use a local copy if one still
exists), then extract. Every restored file is verified against its manifest
SHA-256. Retrieving one archived file from a multi-hundred-GB backup costs
one chunk download, bounded by `chunk_mb` (or the file's own size, for
oversized solo chunks).

## JSON API

Every read command accepts `--json` for structured output (`--status`,
`--list {sync,archive,all}`, `--orphans`); `archive` and `add-sync` also
accept `--json` and print one JSON result object (`archive`'s includes a
`relocatable` list of just-archived files with a live sync twin, for a
caller to offer `retire-sync-twin`). This is the API the SwiftUI app talks
to — see `mac/BackupOrganizerApp/`.

## Code layout

`backup_organizer.py` is a thin launcher (kept as the stable entry point for
the PATH shim, the launchd agent and Backup Status.app); the logic lives in
the `backuporganizer` package:

| Module | Responsibility |
| --- | --- |
| `config.py` | defaults, loading, validation, remote-path normalization |
| `manifest.py` | the manifest state file, atomic saves, v2→v3 migration, the `Member` record |
| `util.py` | logging, hashing, notifications, Finder Trash |
| `scanner.py` | walking sync dirs / the dropzone, diffing against the manifest, `find_sync_twins` |
| `chunks.py` | planning, building and repacking archive chunk zips |
| `proton.py` | every Proton Drive CLI call: upload (chunked and per-file), download, confirm, mkdir |
| `viewer.py` | `--tree` and `--browse` (pure manifest views) |
| `commands.py` | the top-level operations (backup, status, list, orphans, advice, restore, dedupe, archive, retire) |
| `cli.py` | argument parsing, the single-instance lock, dispatch |

Rule of thumb: `commands.py` orchestrates, everything else does one job and
never imports it back (no import cycles: util ← config/manifest ← scanner/
chunks/proton ← viewer/commands ← cli).

## Concurrency & robustness

- A `flock` on `<backup_dir>/.lock` guarantees a manual run and the launchd
  job can never write concurrently; the new `delete-remote` and
  `retire-sync-twin` mutating commands take the same lock.
- Free-space check before building: the sum of all planned archive chunk
  sizes plus `min_free_gb` must fit. Sync uploads stream from `origin`
  directly and use no extra local disk, so they're outside this check.
- All subprocesses use explicit argument lists (no shell) and timeouts
  (6 h for transfers, 15 min otherwise); output is logged on failure.
- Logs rotate at 1 MB (`backup_organizer.log`, 3 backups kept).
