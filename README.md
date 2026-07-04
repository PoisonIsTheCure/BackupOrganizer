# BackupOrganizer

A headless macOS backup manager and smart storage advisor, written in pure-stdlib
Python. It keeps a single timestamped `.zip` of your important directories, offers
a hash-verified "Archive Dropzone" for offloading files, tells you exactly which
local files are safe to delete, and mirrors everything to Proton Drive.

## Features

- **Sync directories** — configured folders are compressed into one archive.
  Re-runs are I/O-efficient: unchanged files cost a single `stat()` call, and only
  new/changed entries are (re)compressed via the system `zip` tool.
- **Archive Dropzone** — drop a file (or folder) in; it is added to the archive,
  the copy *inside the zip* is re-hashed and compared to the original, and only on
  a byte-perfect match is the original moved to the Trash.
- **Manifest state tracking** — `manifest.json` records the path, size, mtime and
  SHA-256 of every archived file, plus the last backup/upload timestamps.
- **Storage advisor** — `--advice DIR` reports which files in a directory are
  byte-identical to a backed-up copy (safe to delete) and which are not.
- **Storage safety** — free-disk-space check before any archive write, optional
  archive size cap before upload, and classified Proton Drive failures
  (storage full vs. login expired vs. network).
- **Proton Drive upload** — the archive and manifest are uploaded with the
  official Proton Drive CLI; older remote archives are pruned only *after* the
  new upload succeeds.
- **Native notifications** — every background run ends in a macOS notification
  (success summary or a specific failure reason).

## Requirements

- macOS (uses `/usr/bin/zip`, `osascript`, Finder Trash, launchd)
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
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for every config key and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the diffing, verification
and upload pipeline works.

## CLI

| Flag | Effect |
| --- | --- |
| *(none)* | Run a backup, then upload to Proton Drive |
| `--status` | Print file count, total size, archive name, last backup/upload |
| `--advice DIR` | Report which files in `DIR` are safely backed up |
| `--init` | Write a default `config.json` and create the dropzone |
| `--no-upload` | Back up locally only |
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
macOS dialog. Keep it in the project folder or drag it to your Desktop/Dock.

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

Everything lives outside the repo, in `~/Backups/BackupOrganizer/`:

```
config.json            your configuration
backup_<timestamp>.zip the archive (name = time of last successful backup)
manifest.json          state: hashes, sizes, timestamps
backup_organizer.log   rotating log
launchd.log            stdout/stderr of scheduled runs
```

## License

[MIT](LICENSE)
