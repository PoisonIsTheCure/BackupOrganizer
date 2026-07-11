# BackupOrganizer.app

A native SwiftUI front end for the [BackupOrganizer](../../README.md) Python
CLI. It's a thin client: every fact and every action goes through
`backup-organizer --json` (see `../../docs/ARCHITECTURE.md`, "JSON API") —
this app owns no backup logic of its own, just the UI.

## Prerequisites

The Python side must already be set up and working from the terminal first
(config written, `proton-drive auth login` done — see the main
[README](../../README.md) and [docs/CONFIGURATION.md](../../docs/CONFIGURATION.md)).
This app is a view onto that, not a replacement for it.

## Development

```sh
swift run          # build + launch, unsigned, for iterating on the UI
swift test          # decodes fixture JSON through the Codable models
```

By default the app shells out to `../../bin/backup-organizer`. If your
checkout lives somewhere else, or you use a non-default `config.json`, open
the app and change the CLI path / config path in Settings — these are
stored in UserDefaults, not hardcoded.

## Building the real app

```sh
Scripts/build_app.sh                 # installs to ~/Applications
Scripts/build_app.sh --applications  # installs to /Applications instead
```

This produces `dist/BackupOrganizer.app`: a release build, a generated
`AppIcon.icns` (`Scripts/generate_icon.swift`), and an ad-hoc code signature
(`codesign --sign -`) — there's no paid Apple Developer ID involved, so:

- **A build you did yourself** launches fine straight from Finder/Launchpad
  — macOS doesn't quarantine files created locally.
- **A build copied from elsewhere** (another Mac, a zip, AirDrop, ...) will
  get Gatekeeper's "can't be opened" dialog on first launch. Right-click the
  app → Open → Open, once; every launch after that is normal. This is
  expected friction for an unsigned personal tool, not a bug.

Re-running `build_app.sh` is safe — it only ever touches its own `dist/`
output and `~/Applications/BackupOrganizer.app` (or `/Applications/...`
with the flag), overwriting the previous build each time.

## What's not here (yet)

A Settings screen for editing `config.json` fields (sync_dirs, exclude
patterns, etc.) beyond the CLI/config path override — for now, edit
`config.json` directly and use the CLI's own `--init`/`add-sync` for the
things this app doesn't expose yet.
