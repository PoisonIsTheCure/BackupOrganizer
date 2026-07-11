# Configuration

`backup_organizer.py --init` writes a default config to
`~/Backups/BackupOrganizer/config.json`. Any key you omit falls back to its
default. Paths may use `~`.

```json
{
  "sync_dirs": ["~/Documents/Sync"],
  "dropzone": "~/Backups/ArchiveDropzone",
  "backup_dir": "~/Backups/BackupOrganizer",
  "manifest": "~/Backups/BackupOrganizer/manifest.json",
  "proton_cli": "/Users/alyz/developement/generalBin/proton-drive",
  "remote_folder": "/my-files/Backups/MacBookAir",
  "chunk_mb": 500,
  "keep_local_chunks": false,
  "min_free_gb": 2,
  "exclude": [
    ".DS_Store", "*.tmp", "._*", ".localized",
    ".venv", "venv", ".v", "node_modules", "__pycache__", "*.pyc", "*.pyo",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".ipynb_checkpoints"
  ]
}
```

| Key | Meaning |
| --- | --- |
| `sync_dirs` | Directories backed up under `Sync/<basename>/…`. Basenames must be unique (their archive paths would collide otherwise — the tool refuses to start). |
| `dropzone` | The Archive Dropzone. Anything placed here is packed into archive chunks under `Archive/…`, uploaded, and — only after the upload is confirmed — moved to the Trash. Must not overlap a sync dir. |
| `backup_dir` | Working directory: chunks being built/awaiting upload, logs, lock file. |
| `manifest` | Path of `manifest.json`. |
| `proton_cli` | Absolute path to the Proton Drive CLI binary. |
| `remote_folder` | Proton Drive folder receiving the chunks + manifest (the full path is created on first run). Proton paths must live inside a namespace root such as `/my-files`; a path without one (e.g. `/Backups/Mac`) is automatically anchored as `/my-files/Backups/Mac`. |
| `chunk_mb` | Target chunk size in MB. Files are first-fit packed up to this cap; a **file larger than the cap gets its own dedicated chunk** — it is never split. Bigger chunks = fewer remote files but more data to re-upload per change and to download per restore. |
| `keep_local_chunks` | `false` (default): local archive chunk zips are deleted once uploaded — Proton Drive is the only copy and disk space is freed. `true`: keep local copies too (uses disk, but restores never download). Sync files are never zipped, so this has no effect on them. |
| `min_free_gb` | Safety margin: a backup aborts (with a notification) if building archive chunks would leave less than this much free disk space. Sync uploads stream straight from the original file and use no extra local disk, so they aren't covered by this check. |
| `exclude` | Glob patterns matched against file **and directory names** (not full paths). Matching items are ignored everywhere: sync dirs, dropzone, and `--advice` scans. The defaults skip regenerable dev artifacts (virtualenvs, `node_modules`, caches); `.git` is deliberately *not* excluded, since unpushed history is irreplaceable. |

## Synced folders

Files under `sync_dirs` are uploaded as **plain files**, mirroring the exact
local directory shape remotely under `Sync/<basename>/…` — no zipping. This
is different from the dropzone/archive, which is chunked into zips (see
below).

**Deleting a synced file locally never deletes its cloud copy.** The next
backup run notices the file is gone and marks it an *orphan* — still on
Proton Drive, no longer tracked as a live local file. Orphans are listed with
`--orphans` and only removed from the cloud with an explicit
`delete-remote ARCNAME`. This is deliberate: local deletion (accidental or
not) should never be able to destroy the only remaining copy of a file.

To move a file from synced to archived and drop the redundant synced copy
(local + cloud), archive it as usual, then once the archive copy is
confirmed uploaded, run `retire-sync-twin ARCHIVE_ARCNAME` — it re-verifies
the synced copy is still byte-identical, deletes it from the cloud, and
moves the local original to the Trash. This is a deliberate, explicit
action; nothing does this automatically (see the GUI, which prompts before
calling it).

## Multiple configurations

Every command accepts `--config PATH`, so you can keep independent backup sets
(e.g. a work set and a personal set), each with its own manifest, chunks and
remote folder. The sets must use different `backup_dir` values.

## Dropzone behavior details

- Hidden files (`.foo`) at the top level are ignored.
- An entry (file or folder) is skipped for one run if anything inside it was
  modified in the last 30 seconds — this avoids archiving half-copied files.
- Originals are moved to the Trash **only after** their chunk's upload has
  been confirmed. With `--no-upload`, or after a failed upload, everything
  stays where it is and the next run finishes the job.
- Name collisions with already-archived content get a timestamp suffix
  (`report.pdf` → `report_20260704_213000.pdf`) so nothing is overwritten.
- A dropped folder is only trashed when **every** file inside it is uploaded.
- Content that is already archived under another name is never stored twice;
  the dropped file is simply trashed once its existing chunk is confirmed.
- If the dropped content is byte-identical to a file in a synced area (i.e.
  it was *copied* there rather than moved), the sync-area original is
  **not** touched automatically — see "Synced folders" above for the
  explicit `retire-sync-twin` step that drops the redundant sync copy.

## Large files (videos, disk images, …)

Files above `chunk_mb` are placed alone in their own chunk and stored inside
the zip **without recompression** when the format is already compressed
(`.mp4`, `.mov`, `.heic`, `.jpg`, `.zip`, …) — archiving a big video is
essentially a copy plus a checksum, not a slow deflate. Restoring it later
downloads exactly that one chunk.
