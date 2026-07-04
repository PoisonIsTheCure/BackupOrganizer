#!/usr/bin/env python3
"""BackupOrganizer launcher.

Kept as a stable entry point for the PATH shim, the launchd agent, and
Backup Status.app. All logic lives in the `backuporganizer` package next to
this file — see backuporganizer/__init__.py for the module map.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backuporganizer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
