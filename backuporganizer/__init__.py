"""BackupOrganizer — macOS backup manager and smart storage advisor.

Packs configured "Sync" directories and an "Archive Dropzone" into
size-capped zip chunks, uploads them to Proton Drive, then removes the
local copies to free disk space. State lives in a SHA-256 manifest.

Module map:
    config    configuration loading, validation, path normalization
    manifest  the manifest state file and the Member record
    util      logging, hashing, notifications, Trash, shared helpers
    scanner   walking sync dirs / the dropzone and diffing against the manifest
    chunks    planning, building, repacking and verifying chunk zips
    proton    every interaction with the Proton Drive CLI
    viewer    --tree and --browse (terminal / HTML views of the manifest)
    commands  the top-level operations (backup, status, advice, restore, dedupe)
    cli       argument parsing, locking, dispatch
"""

__version__ = "2.0.0"
