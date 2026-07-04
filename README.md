# BackupOrganizer

A headless macOS backup manager and smart storage advisor, written in pure-stdlib
Python. It packs your important directories and an "Archive Dropzone" into
size-capped zip **chunks**, uploads them to Proton Drive, then deletes the local
copies to free disk space — and can restore any single file by downloading only
the one chunk that contains it.

## Features

- **Chunked backups** — data is packed into ~500 MB zip chunks. A changed file
  only rebuilds and re-uploads *its own* chunk, never the whole backup; files
  larger than the cap get a dedicated chunk of their own (never split), and
  already-compressed formats (video, photos, audio) are stored without
  recompression.
- **Space freeing by design** — once a chunk's upload is confirmed, the local
  copy is deleted. Proton Drive is the primary store, not a mirror.
- **Archive Dropzone** — drop a file (or folder) in; it is packed into a chunk,
  hash-verified inside the zip, uploaded — and only after the remote copy is
  confirmed is the original moved to the Trash.
- **Single-file restore** — `--restore invoice.pdf` finds the file in the
  manifest, downloads just its chunk, extracts and hash-verifies it.
- **Backup browser** — `--browse` renders the whole backup as an interactive
  HTML page (collapsible folders, live search, sizes, chunk badges) built
  purely from the manifest — browsing costs zero downloads. Every folder and
  file has a ⬇ button that copies the matching restore command to the
  clipboard. `--tree` prints the same structure in the terminal.
- **Manifest state tracking** — `manifest.json` records the path, size, mtime,
  SHA-256 and chunk of every file. Diffing is I/O-efficient: unchanged files
  cost one `stat()` call, zero reads.
- **Storage advisor** — `--advice DIR` reports which files in a directory are
  byte-identical to an *uploaded* copy (safe to delete) and which are not.
- **Deduplication** — `--dedupe` finds files stored more than once (by SHA-256)
  and asks, group by group, which copy to keep. Removed sync copies go to the
  local Trash; removed archive copies are repacked out of their chunks. Every
  removal is hash-verified first.
- **Storage safety** — free-disk-space check before building chunks, remote
  size confirmation after upload, and classified Proton Drive failures
  (storage full vs. login expired vs. network).
- **Native notifications** — every background run ends in a macOS notification
  (success summary or a specific failure reason).

## Requirements

- macOS (uses `osascript`, Finder Trash, launchd)
- Python 3.11+ (no third-party packages)
- [Proton Drive CLI](https://proton.me/drive), authenticated via
  `proton-drive auth login`

## Quick start

```sh
# 1. Create the default config and directories
python3 backup_organizer.py --init

# 2. Edit ~/Backups/BackupOrganizer/config.json — at minimum, set sync_dirs

# 3. First backup (add --no-upload to skip Proton Drive)
python3 backup_organizer.py --verbose

# 4. Check on things any time
python3 backup_organizer.py --status
python3 backup_organizer.py --advice ~/Downloads

# 5. Get a file back (downloads only the chunk that holds it)
python3 backup_organizer.py --restore holiday.mov
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for every config key and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the diffing, verification
and upload pipeline works.

## CLI

| Flag | Effect |
| --- | --- |
| *(none)* | Run a backup: build changed chunks, upload, free local space |
| `--status` | Print file/chunk counts, sizes, pending uploads, last backup/upload |
| `--advice DIR` | Report which files in `DIR` are safely backed up |
| `--tree [PREFIX]` | Print the backed-up file tree, optionally under a path prefix |
| `--browse [OUT]` | Generate an interactive HTML backup browser and open it |
| `--restore NAME` | Restore file(s) by name/substring/glob, or a whole folder with a trailing `/` |
| `--restore-all` | Download and restore the entire backup (see `--dest`) |
| `--dedupe [MIN_MB]` | Interactively keep one copy of duplicated files (default: ≥ 1 MB) |
| `--dest DIR` | Destination for `--restore` (default: `~/Downloads/BackupOrganizer-Restore`) |
| `--init` | Write a default `config.json` and create the dropzone |
| `--no-upload` | Build chunks locally only; nothing is deleted or trashed |
| `--dry-run` | Show what would change without writing anything |
| `--no-notify` | Suppress macOS notifications |
| `--config PATH` | Use an alternate config file |
| `--verbose` | Chatty console output |

Exit codes: `0` success, `1` failure, `2` configuration error.

## Command-line launcher

`bin/backup-organizer` is a tiny shim so the tool works from anywhere:

```sh
cp bin/backup-organizer ~/developement/generalBin/   # any dir on your PATH
chmod +x ~/developement/generalBin/backup-organizer
backup-organizer --status
```

## Status dialog (double-click app)

Build the native dialog app once:

```sh
osacompile -o "Backup Status.app" "Backup Status.applescript"
```

Double-clicking **Backup Status.app** shows the `--status` report in a native
macOS dialog with two action buttons: **Run Backup** kicks off a full backup
in the background (a notification arrives when it finishes), and
**Browse Files** opens the interactive HTML backup browser. Keep the app in
the project folder or drag it to your Desktop/Dock.

## Daily background runs with launchd

1. Adjust paths/schedule in `com.alyz.backuporganizer.plist` if needed
   (default: every day at 21:00).
2. Install and start it:

   ```sh
   cp com.alyz.backuporganizer.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alyz.backuporganizer.plist
   ```

3. Trigger a test run right now:

   ```sh
   launchctl kickstart -k gui/$(id -u)/com.alyz.backuporganizer
   ```

4. To uninstall:

   ```sh
   launchctl bootout gui/$(id -u)/com.alyz.backuporganizer
   rm ~/Library/LaunchAgents/com.alyz.backuporganizer.plist
   ```

### macOS permissions

- If your sync dirs live in `~/Documents`, `~/Desktop`, or other protected
  locations, grant **Full Disk Access** to `python3` (System Settings →
  Privacy & Security → Full Disk Access), otherwise the launchd job will see
  empty directories.
- The first dropzone deletion triggers an **Automation** prompt ("python wants
  to control Finder") — approve it once.
- Notifications from background jobs may need **Allow Notifications** for
  Script Editor / osascript the first time one fires.

## Data layout

Locally (outside the repo), `~/Backups/BackupOrganizer/` holds:

```
config.json            your configuration
sync-00001.zip, ...    chunks being built or awaiting upload (deleted once uploaded)
manifest.json          state: hashes, sizes, chunk assignments, timestamps
backup_organizer.log   rotating log
launchd.log            stdout/stderr of scheduled runs
```

On Proton Drive, everything sits in one folder (default `/my-files/Backups/MacBookAir`):

```
sync-00001.zip         chunks of the sync directories
arch-00002.zip         write-once chunks of archived dropzone files
manifest.json          always the latest state
```

## License

[MIT](LICENSE)
