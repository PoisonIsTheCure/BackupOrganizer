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
| `keep_local_chunks` | `false` (default): local chunk zips are deleted once uploaded — Proton Drive is the only copy and disk space is freed. `true`: keep local copies too (uses disk, but restores never download). |
| `min_free_gb` | Safety margin: a backup aborts (with a notification) if building its chunks would leave less than this much free disk space. |
| `exclude` | Glob patterns matched against file **and directory names** (not full paths). Matching items are ignored everywhere: sync dirs, dropzone, and `--advice` scans. The defaults skip regenerable dev artifacts (virtualenvs, `node_modules`, caches); `.git` is deliberately *not* excluded, since unpushed history is irreplaceable. |

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
- **No duplicates on archive**: if the dropped content is byte-identical to a
  file in a synced area (i.e. it was *copied* there), the sync-area original
  is also moved to the Trash once the archive copy is confirmed uploaded —
  only the archived copy stays. Content that is already archived under
  another name is never stored twice; the dropped file is simply trashed
  once its existing chunk is confirmed. A sync copy that changed since its
  backup is never touched.

## Large files (videos, disk images, …)

Files above `chunk_mb` are placed alone in their own chunk and stored inside
the zip **without recompression** when the format is already compressed
(`.mp4`, `.mov`, `.heic`, `.jpg`, `.zip`, …) — archiving a big video is
essentially a copy plus a checksum, not a slow deflate. Restoring it later
downloads exactly that one chunk.
