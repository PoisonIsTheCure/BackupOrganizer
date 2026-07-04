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
  "remote_folder": "/Backups",
  "remote_keep": 1,
  "min_free_gb": 2,
  "max_zip_gb": 0,
  "exclude": [".DS_Store", "*.tmp", "._*", ".localized"]
}
```

| Key | Meaning |
| --- | --- |
| `sync_dirs` | Directories mirrored into the archive under `Sync/<basename>/…`. Basenames must be unique (their archive paths would collide otherwise — the tool refuses to start). |
| `dropzone` | The Archive Dropzone. Anything placed here is archived under `Archive/…`, hash-verified, then moved to the Trash. Must not overlap a sync dir. |
| `backup_dir` | Where the `backup_<timestamp>.zip`, logs and lock file live. |
| `manifest` | Path of `manifest.json`. |
| `proton_cli` | Absolute path to the Proton Drive CLI binary. |
| `remote_folder` | Proton Drive folder receiving the archive + manifest (created on first run). |
| `remote_keep` | How many remote `backup_*.zip` files to keep. Older ones are moved to Proton's trash only **after** a new upload succeeds. Minimum 1. |
| `min_free_gb` | Safety margin: a backup aborts (with a notification) if writing it would leave less than this much free disk space. |
| `max_zip_gb` | If > 0, the upload is skipped (with a "storage" notification) when the archive exceeds this size. `0` = unlimited. |
| `exclude` | Glob patterns matched against file **and directory names** (not full paths). Matching items are ignored everywhere: sync dirs, dropzone, and `--advice` scans. |

## Multiple configurations

Every command accepts `--config PATH`, so you can keep independent backup sets
(e.g. a work set and a personal set), each with its own manifest, archive and
remote folder. Note the two sets must use different `backup_dir` values.

## Dropzone behavior details

- Hidden files (`.foo`) at the top level are ignored.
- An entry (file or folder) is skipped for one run if anything inside it was
  modified in the last 30 seconds — this avoids archiving half-copied files.
- Name collisions with already-archived content get a timestamp suffix
  (`report.pdf` → `report_20260704_213000.pdf`) so nothing is overwritten.
- A dropped folder is only trashed when **every** file inside it verified.
