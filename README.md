# BackupOrganizer

A macOS backup manager and smart storage advisor: a pure-stdlib Python engine
(CLI + JSON API) with a native SwiftUI app on top. It treats two kinds of
folders differently:

- **Synced** folders (`sync_dirs`) upload as **plain files, mirroring the
  local directory shape** on Proton Drive — no zipping. Deleting a synced
  file locally never deletes its cloud copy automatically; that's always a
  deliberate, explicit action (see "Synced folders" below).
- **Archived** folders (the dropzone) are packed into size-capped zip
  **chunks**, uploaded, and — only once the remote copy is confirmed — the
  local original is moved to the Trash. Any single archived file restores by
  downloading just the one chunk that contains it.

## Features

- **Two storage models, one tool** — synced content mirrors 1:1 to the cloud
  as plain files; archived content is chunked into zips. See
  `docs/ARCHITECTURE.md` for the full design and `docs/CONFIGURATION.md` for
  "Synced folders".
- **Chunked archiving** — dropzone data is packed into ~500 MB zip chunks;
  files larger than the cap get a dedicated chunk of their own (never split),
  and already-compressed formats (video, photos, audio) are stored without
  recompression.
- **Space freeing by design** — once an archive chunk's upload is confirmed,
  the local zip is deleted. Sync uploads stream straight from the original
  file and never touch local disk at all. Proton Drive is the primary store
  for archived data, not a mirror.
- **Archive Dropzone** — drop a file (or folder) in; it is packed into a
  chunk, hash-verified inside the zip, uploaded — and only after the remote
  copy is confirmed is the original moved to the Trash.
- **Orphans, not silent deletion** — deleting a synced file locally marks its
  cloud copy an *orphan* (`--orphans`) rather than deleting it; only an
  explicit `delete-remote ARCNAME` removes it. Moving something from synced
  to archived is similarly explicit: `retire-sync-twin` drops the redundant
  synced copy (local + cloud) once the archived copy is confirmed uploaded —
  it's never automatic.
- **Single-file restore** — `--restore invoice.pdf` finds the file in the
  manifest (synced or archived) and downloads only what's needed to restore
  it, then hash-verifies it.
- **Backup browser** — `--browse` renders the whole backup as an interactive
  HTML page (collapsible folders, live search, sizes, sync/archive badges)
  built purely from the manifest — browsing costs zero downloads. Every
  folder and file has a ⬇ button that copies the matching restore command to
  the clipboard. `--tree` prints the same structure in the terminal.
- **JSON API** — `--json` on `--status`/`--list`/`--orphans`/`archive`/
  `add-sync`, plus streaming NDJSON progress on `run --json`. This is what
  the SwiftUI app talks to; see `mac/BackupOrganizerApp/`.
- **Manifest state tracking** — `manifest.json` records the path, size,
  mtime, SHA-256, and upload state of every file. Diffing is I/O-efficient:
  unchanged files cost one `stat()` call, zero reads.
- **Storage advisor** — `--advice DIR` reports which files in a directory are
  byte-identical to an *uploaded* copy (safe to delete locally) and which
  are not.
- **Deduplication** — `--dedupe` finds files stored more than once (by
  SHA-256) and asks, group by group, which copy to keep. Removed sync copies
  go to the local Trash (their cloud copy becomes an orphan); removed
  archive copies are repacked out of their chunks. Every removal is
  hash-verified first.
- **Storage safety** — free-disk-space check before building archive chunks,
  remote size/hash confirmation after every upload, and classified Proton
  Drive failures (storage full vs. login expired vs. network).
- **Native notifications** — every background run ends in a macOS
  notification (success summary or a specific failure reason).

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

### Commands

```sh
backup-organizer run                             # full cycle: diff, upload sync files, chunk+upload archive
backup-organizer archive ~/Movies/old            # move into the dropzone + run the cycle
backup-organizer add-sync ~/Projects             # add a folder to sync_dirs + run the cycle
backup-organizer delete-remote Sync/Docs/old.md  # permanently delete an orphaned synced file's cloud copy
backup-organizer retire-sync-twin Archive/a.jpg  # drop the redundant synced copy of an archived file
```

`run` is the same as invoking `backup-organizer` with no arguments. `archive`
takes any number of files/folders and skips the dropzone settle delay (you
just told it the files are complete). `add-sync` validates the new folders
(must exist, unique basenames, no dropzone overlap) before touching the
config. `archive`/`add-sync` accept `--no-run` to stage only, and honor
`--no-upload`. `delete-remote` and `retire-sync-twin` are deliberately manual
— see "Synced folders" in `docs/CONFIGURATION.md`.

**Global flags must come before the subcommand** (argparse quirk): write
`backup-organizer --json run`, not `backup-organizer run --json`.

### Flags

| Flag | Effect |
| --- | --- |
| *(none)* / `run` | Run a backup: upload sync files, build+upload changed archive chunks |
| `--status` | Print file counts, sizes, pending uploads, orphans, last backup/upload |
| `--list [sync\|archive\|all]` | List backed-up files |
| `--orphans` | List synced files deleted locally but still in the cloud |
| `--advice DIR` | Report which files in `DIR` are safely backed up |
| `--tree [PREFIX]` | Print the backed-up file tree, optionally under a path prefix |
| `--browse [OUT]` | Generate an interactive HTML backup browser and open it |
| `--restore NAME` | Restore file(s) by name/substring/glob, or a whole folder with a trailing `/` |
| `--restore-all` | Download and restore the entire backup (see `--dest`) |
| `--dedupe [MIN_MB]` | Interactively keep one copy of duplicated files (default: ≥ 1 MB) |
| `--dest DIR` | Destination for `--restore` (default: `~/Downloads/BackupOrganizer-Restore`) |
| `--init` | Write a default `config.json` and create the dropzone |
| `--json` | Emit machine-readable JSON (status/list/orphans/archive/add-sync/run and both manual commands) |
| `--no-upload` | Don't upload; nothing is deleted or trashed |
| `--dry-run` | Show what would change without writing anything |
| `--no-notify` | Suppress macOS notifications |
| `--config PATH` | Use an alternate config file |
| `--verbose` | Chatty console output |

Exit codes: `0` success, `1` failure, `2` configuration error.

## macOS app

`mac/BackupOrganizerApp/` is a native SwiftUI app — a thin client over the
CLI's `--json` output, launchable from Launchpad/Spotlight like any other
Mac app, with a live-updating Dashboard, Synced/Archived browsers, and
confirmation dialogs for the two deliberate manual actions (delete a synced
file's cloud copy, retire a synced copy once its archive twin is uploaded).
No Xcode project needed — it's a Swift Package:

```sh
cd mac/BackupOrganizerApp
swift run                    # build + launch for development
Scripts/build_app.sh         # build the real .app, install to ~/Applications
```

See `mac/BackupOrganizerApp/README.md` for details (CLI/config path
overrides, the Gatekeeper right-click-open note for ad-hoc-signed builds).

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
arch-00001.zip, ...    archive chunks being built or awaiting upload (deleted once uploaded)
manifest.json          state: hashes, sizes, chunk/upload assignments, orphans, timestamps
backup_organizer.log   rotating log
launchd.log            stdout/stderr of scheduled runs
```

Sync files are never written to local disk beyond the manifest — they upload
straight from their original location.

On Proton Drive, everything sits under `remote_folder` (default
`/my-files/Backups/MacBookAir`):

```
Sync/Documents/notes.md   synced files, mirroring the local directory shape 1:1
arch-00001.zip             write-once chunks of archived dropzone files
manifest.json               always the latest state
```

## License

[MIT](LICENSE)
