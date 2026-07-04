"""Configuration: defaults, loading, validation, and remote-path rules."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/Backups/BackupOrganizer/config.json").expanduser()

# The Proton Drive CLI only accepts paths inside one of its root namespaces
# (`filesystem list /` shows them); a bare folder path like /Backups is
# rejected with: Path "/Backups" not supported.
PROTON_NAMESPACES = {
    "my-files", "devices", "shared-by-me", "shared-with-me", "albums",
    "photos", "photos-shared-by-me", "photos-shared-with-me",
}

DEFAULT_CONFIG = {
    "sync_dirs": ["~/Documents/Sync"],
    "dropzone": "~/Backups/ArchiveDropzone",
    "backup_dir": "~/Backups/BackupOrganizer",
    "manifest": "~/Backups/BackupOrganizer/manifest.json",
    "proton_cli": "/Users/alyz/developement/generalBin/proton-drive",
    "remote_folder": "/my-files/Backups/MacBookAir",
    "chunk_mb": 500,
    "keep_local_chunks": False,
    "retire_sync_copies": True,
    "min_free_gb": 2,
    "exclude": [
        ".DS_Store", "*.tmp", "._*", ".localized",
        # regenerable dev directories/files — cheap to rebuild, churn-heavy
        ".venv", "venv", ".v", "node_modules", "__pycache__", "*.pyc", "*.pyo",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".ipynb_checkpoints",
    ],
}


class ConfigError(Exception):
    """The config file is missing, unparseable, or self-contradictory."""


def normalize_remote_folder(raw: str) -> str:
    """Anchor the remote folder inside a Proton Drive namespace.

    "/Backups/Mac" and "Backups/Mac" both become "/my-files/Backups/Mac";
    a path that already names a namespace is kept as-is.
    """
    parts = [p for p in raw.strip("/").split("/") if p]
    if not parts:
        return "/my-files"
    if parts[0] not in PROTON_NAMESPACES:
        parts.insert(0, "my-files")
    return "/" + "/".join(parts)


@dataclass
class Config:
    """Validated runtime configuration. See docs/CONFIGURATION.md."""

    sync_dirs: list[Path]
    dropzone: Path
    backup_dir: Path
    manifest_path: Path
    proton_cli: str
    remote_folder: str
    chunk_mb: float
    keep_local_chunks: bool
    retire_sync_copies: bool
    min_free_gb: float
    exclude: list[str]

    @property
    def chunk_cap(self) -> int:
        """Target chunk size in bytes; files >= this get a solo chunk."""
        return max(1, int(self.chunk_mb * 1024**2))

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Read the JSON config at path, fill defaults, validate, return."""
        if not path.is_file():
            raise ConfigError(
                f"Config not found at {path}. Run with --init to create a default one."
            )
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: dict) -> "Config":
        """Build and validate a Config from a raw dict (defaults filled in)."""
        raw = {**DEFAULT_CONFIG, **raw}
        cfg = cls(
            sync_dirs=[Path(p).expanduser() for p in raw["sync_dirs"]],
            dropzone=Path(raw["dropzone"]).expanduser(),
            backup_dir=Path(raw["backup_dir"]).expanduser(),
            manifest_path=Path(raw["manifest"]).expanduser(),
            proton_cli=str(raw["proton_cli"]),
            remote_folder=normalize_remote_folder(str(raw["remote_folder"])),
            chunk_mb=float(raw["chunk_mb"]),
            keep_local_chunks=bool(raw["keep_local_chunks"]),
            retire_sync_copies=bool(raw["retire_sync_copies"]),
            min_free_gb=float(raw["min_free_gb"]),
            exclude=list(raw["exclude"]),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Reject configs whose archive paths would collide or overlap."""
        if not self.sync_dirs and not self.dropzone:
            raise ConfigError("Nothing to back up: no sync_dirs and no dropzone configured.")
        seen: dict[str, Path] = {}
        for d in self.sync_dirs:
            if d.name in seen:
                raise ConfigError(
                    f"Sync dirs {seen[d.name]} and {d} share the basename {d.name!r}; "
                    "their archive paths would collide. Rename one of them."
                )
            seen[d.name] = d
        for d in self.sync_dirs:
            if self.dropzone == d or self.dropzone in d.parents or d in self.dropzone.parents:
                raise ConfigError(f"Dropzone {self.dropzone} overlaps sync dir {d}.")

    def is_excluded(self, name: str) -> bool:
        """True if a file/directory *name* matches any exclude pattern."""
        return any(fnmatch.fnmatch(name, pat) for pat in self.exclude)

    def chunk_path(self, name: str) -> Path:
        """Local path of a chunk zip while it exists on disk."""
        return self.backup_dir / name

    def remote_path(self, name: str) -> str:
        """Full Proton Drive path of a file in the remote folder."""
        return f"{self.remote_folder}/{name}"
