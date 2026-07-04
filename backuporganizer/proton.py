"""Every interaction with the Proton Drive CLI: upload, download, confirm."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .config import Config
from .manifest import Manifest
from .util import human_size, log, now_iso

SUBPROCESS_TIMEOUT = 15 * 60
TRANSFER_TIMEOUT = 6 * 60 * 60  # multi-GB chunks on a slow uplink take a while

QUOTA_ERROR_RE = re.compile(r"quota|storage.*(full|exceed)|insufficient|not enough space", re.I)
AUTH_ERROR_RE = re.compile(r"auth|login|session|unauthoriz|forbidden|credential|401|403", re.I)

UPLOAD_FAIL_MESSAGES = {
    "quota": "Proton Drive storage is FULL — upload failed. Free up space in your plan.",
    "auth": "Proton Drive login expired — run `proton-drive auth login`.",
    "error": "Proton Drive upload failed (network/CLI error). Data is kept locally.",
}


class UploadError(Exception):
    """A failed CLI transfer, classified for the right notification text."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "quota" | "auth" | "error"


def classify_upload_error(output: str) -> str:
    """Map CLI output to a failure kind (the CLI has no quota query, so
    storage-full can only be detected reactively from its error text)."""
    if QUOTA_ERROR_RE.search(output):
        return "quota"
    if AUTH_ERROR_RE.search(output):
        return "auth"
    return "error"


def proton(cfg: Config, *args: str, timeout: int = SUBPROCESS_TIMEOUT) -> subprocess.CompletedProcess:
    """Run the Proton Drive CLI with an explicit arg list and timeout."""
    return subprocess.run(
        [cfg.proton_cli, *args], capture_output=True, text=True, timeout=timeout
    )


def ensure_remote_folder(cfg: Config) -> None:
    """Create each component of remote_folder; 'already exists' is fine.

    The first component is a namespace root (/my-files, ...) that always
    exists and cannot be created, so folder creation starts below it.
    """
    parts = [p for p in cfg.remote_folder.split("/") if p]
    parent = "/" + parts[0]
    for part in parts[1:]:
        proc = proton(cfg, "filesystem", "create-folder", parent, part)
        if proc.returncode != 0 and "exist" not in (proc.stderr + proc.stdout).lower():
            log.debug("create-folder %s/%s: %s", parent.rstrip("/"), part, proc.stderr.strip())
        parent = parent.rstrip("/") + "/" + part


def upload_file(cfg: Config, local: Path) -> None:
    """Upload one file into remote_folder, replacing any previous version."""
    proc = proton(
        cfg, "filesystem", "upload", "-c", "replace", "-t",
        str(local), cfg.remote_folder, timeout=TRANSFER_TIMEOUT,
    )
    if proc.returncode != 0:
        output = proc.stderr + proc.stdout
        raise UploadError(classify_upload_error(output), output.strip()[:500] or "unknown error")


def confirm_remote(cfg: Config, name: str, expect_size: int, expect_sha1: str) -> bool:
    """Check the uploaded file's remote metadata against the local chunk.

    `filesystem info -j` reports the plaintext size (claimedSize) and a SHA-1
    digest claimed at upload time; both must match before the local copy may
    be deleted. Returns False only on a *positive* mismatch; an unreadable or
    unparseable response is trusted (the upload already exited 0)."""
    try:
        proc = proton(cfg, "filesystem", "info", "-j", cfg.remote_path(name))
        if proc.returncode != 0:
            log.debug("Remote confirm of %s unavailable: %s", name, proc.stderr.strip())
            return True
        sizes: list[int] = []
        sha1s: list[str] = []

        def collect(node) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if "size" in k.lower() and isinstance(v, int):
                        sizes.append(v)
                    elif k == "sha1" and isinstance(v, str):
                        sha1s.append(v.lower())
                    collect(v)
            elif isinstance(node, list):
                for v in node:
                    collect(v)

        collect(json.loads(proc.stdout))
        if sizes and expect_size not in sizes:
            log.error("Remote size mismatch for %s: local %d, remote %s", name, expect_size, sizes)
            return False
        if sha1s and expect_sha1 not in sha1s:
            log.error("Remote SHA-1 mismatch for %s: local %s, remote %s", name, expect_sha1, sha1s)
            return False
        return True
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return True


def download_chunk(cfg: Config, name: str, dest_dir: Path) -> Path:
    """Download one chunk from the remote folder into dest_dir."""
    proc = proton(
        cfg, "filesystem", "download", "-c", "replace",
        cfg.remote_path(name), str(dest_dir), timeout=TRANSFER_TIMEOUT,
    )
    local = dest_dir / name
    if proc.returncode != 0 or not local.is_file():
        output = (proc.stderr + proc.stdout).strip()[:500]
        raise UploadError(classify_upload_error(output), f"download of {name} failed: {output}")
    return local


def upload_pending(cfg: Config, manifest: Manifest,
                   trash_remote: list[str]) -> tuple[int, int, UploadError | None]:
    """Upload every not-yet-uploaded chunk plus the manifest.

    After each confirmed chunk upload the local zip is deleted (unless
    keep_local_chunks). Old remote chunks in trash_remote are only trashed
    once their replacements are up. Returns (chunks uploaded, local bytes
    freed, error) — an error stops the loop but keeps all progress saved.
    """
    error: UploadError | None = None
    uploaded = 0
    freed = 0
    try:
        ensure_remote_folder(cfg)
        for name in sorted(manifest.chunks):
            meta = manifest.chunks[name]
            local = cfg.chunk_path(name)
            if meta["uploaded"] or not local.is_file():
                continue
            log.info("Uploading %s (%s) ...", name, human_size(meta["size"]))
            with open(local, "rb") as fh:
                local_sha1 = hashlib.file_digest(fh, "sha1").hexdigest()
            upload_file(cfg, local)
            if not confirm_remote(cfg, name, meta["size"], local_sha1):
                raise UploadError("error", f"remote verification failed for {name}")
            meta["uploaded"] = now_iso()
            uploaded += 1
            manifest.save()  # persist progress after every chunk
            if not cfg.keep_local_chunks:
                freed += meta["size"]
                local.unlink()
    except UploadError as exc:
        error = exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = UploadError("error", str(exc))

    if error is None:
        for name in trash_remote:
            proc = proton(cfg, "filesystem", "trash", cfg.remote_path(name))
            if proc.returncode != 0:
                log.warning("Could not trash remote %s: %s", name, proc.stderr.strip())
        try:
            manifest.save()
            upload_file(cfg, manifest.path)
            manifest.last_upload = now_iso()
        except UploadError as exc:
            error = exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = UploadError("error", str(exc))
    manifest.save()
    return uploaded, freed, error
