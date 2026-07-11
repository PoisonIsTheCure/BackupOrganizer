"""Every interaction with the Proton Drive CLI: upload, download, confirm."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .config import Config
from .manifest import Manifest, Member
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


def ensure_remote_dirs(cfg: Config, manifest: Manifest, remote_dirs: set[str]) -> None:
    """Create every ancestor folder of remote_dirs that isn't already known.

    Folders are created top-down (a child's parent must exist first) and
    every newly-created path is recorded in manifest.remote_dirs so later
    runs skip it entirely instead of re-issuing create-folder calls for a
    largely static sync tree.
    """
    known = set(manifest.remote_dirs) | {cfg.remote_folder}
    needed: set[str] = set()
    for d in remote_dirs:
        parts = [p for p in d[len(cfg.remote_folder):].strip("/").split("/") if p]
        parent = cfg.remote_folder
        for part in parts:
            parent = parent.rstrip("/") + "/" + part
            needed.add(parent)
    for path in sorted(needed - known, key=lambda p: p.count("/")):
        parent, _, name = path.rpartition("/")
        proc = proton(cfg, "filesystem", "create-folder", parent, name)
        if proc.returncode != 0 and "exist" not in (proc.stderr + proc.stdout).lower():
            log.debug("create-folder %s/%s: %s", parent, name, proc.stderr.strip())
        manifest.remote_dirs.append(path)
        known.add(path)


def upload_file(cfg: Config, local: Path, remote_parent: str | None = None) -> None:
    """Upload one file into remote_parent (default remote_folder), replacing
    any previous version."""
    proc = proton(
        cfg, "filesystem", "upload", "-c", "replace", "-t",
        str(local), remote_parent or cfg.remote_folder, timeout=TRANSFER_TIMEOUT,
    )
    if proc.returncode != 0:
        output = proc.stderr + proc.stdout
        raise UploadError(classify_upload_error(output), output.strip()[:500] or "unknown error")


def upload_files(cfg: Config, locals_: list[Path], remote_parent: str) -> None:
    """Upload several files into the same remote_parent in one CLI call."""
    proc = proton(
        cfg, "filesystem", "upload", "-c", "replace", "-t",
        *[str(p) for p in locals_], remote_parent, timeout=TRANSFER_TIMEOUT,
    )
    if proc.returncode != 0:
        output = proc.stderr + proc.stdout
        raise UploadError(classify_upload_error(output), output.strip()[:500] or "unknown error")


def confirm_remote(cfg: Config, remote_path: str, expect_size: int, expect_sha1: str) -> bool:
    """Check the uploaded file's remote metadata against the local copy.

    `filesystem info -j` reports the plaintext size (claimedSize) and a SHA-1
    digest claimed at upload time; both must match before the local copy may
    be deleted. Returns False only on a *positive* mismatch; an unreadable or
    unparseable response is trusted (the upload already exited 0)."""
    try:
        proc = proton(cfg, "filesystem", "info", "-j", remote_path)
        if proc.returncode != 0:
            log.debug("Remote confirm of %s unavailable: %s", remote_path, proc.stderr.strip())
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
            log.error("Remote size mismatch for %s: local %d, remote %s", remote_path, expect_size, sizes)
            return False
        if sha1s and expect_sha1 not in sha1s:
            log.error("Remote SHA-1 mismatch for %s: local %s, remote %s", remote_path, expect_sha1, sha1s)
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


def download_file(cfg: Config, remote_path: str, dest_dir: Path) -> Path:
    """Download one plain mirrored file (a synced file, not a chunk) into
    dest_dir. Returns the downloaded path, named after the remote leaf name."""
    proc = proton(
        cfg, "filesystem", "download", "-c", "replace",
        remote_path, str(dest_dir), timeout=TRANSFER_TIMEOUT,
    )
    leaf = remote_path.rstrip("/").rsplit("/", 1)[-1]
    local = dest_dir / leaf
    if proc.returncode != 0 or not local.is_file():
        output = (proc.stderr + proc.stdout).strip()[:500]
        raise UploadError(classify_upload_error(output), f"download of {remote_path} failed: {output}")
    return local


def upload_pending(cfg: Config, manifest: Manifest, trash_remote: list[str],
                   on_progress: Callable[[dict], None] | None = None,
                   ) -> tuple[int, int, UploadError | None]:
    """Upload every not-yet-uploaded chunk plus the manifest.

    After each confirmed chunk upload the local zip is deleted (unless
    keep_local_chunks). Old remote chunks in trash_remote are only trashed
    once their replacements are up. Returns (chunks uploaded, local bytes
    freed, error) — an error stops the loop but keeps all progress saved.
    on_progress, if given, is called with a JSON-serializable dict after
    every confirmed chunk upload (for `run --json` NDJSON streaming).
    """
    error: UploadError | None = None
    uploaded = 0
    freed = 0
    try:
        ensure_remote_folder(cfg)
        pending = sorted(
            name for name, meta in manifest.chunks.items()
            if not meta["uploaded"] and cfg.chunk_path(name).is_file()
        )
        for name in pending:
            meta = manifest.chunks[name]
            local = cfg.chunk_path(name)
            log.info("Uploading %s (%s) ...", name, human_size(meta["size"]))
            with open(local, "rb") as fh:
                local_sha1 = hashlib.file_digest(fh, "sha1").hexdigest()
            upload_file(cfg, local)
            if not confirm_remote(cfg, cfg.remote_path(name), meta["size"], local_sha1):
                raise UploadError("error", f"remote verification failed for {name}")
            meta["uploaded"] = now_iso()
            uploaded += 1
            manifest.save()  # persist progress after every chunk
            if not cfg.keep_local_chunks:
                freed += meta["size"]
                local.unlink()
            if on_progress:
                on_progress({"event": "chunk_uploaded", "name": name,
                            "index": uploaded, "total": len(pending)})
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


CONFIRM_WORKERS = 4  # confirm_remote is one subprocess per file, almost pure
                     # network wait — safe to run several at once; the actual
                     # uploads stay sequential (bandwidth-bound, not helped
                     # by concurrency the way a metadata check is).


def upload_sync_files(cfg: Config, manifest: Manifest,
                      on_progress: Callable[[dict], None] | None = None,
                      ) -> tuple[int, UploadError | None]:
    """Upload every not-yet-uploaded sync file as a plain file, mirroring the
    local directory shape remotely — no zipping, no chunk building, nothing
    written to local disk beyond the manifest.

    Scans manifest.files for source=="sync" entries with no `uploaded`
    timestamp (same resumable pattern as upload_pending's chunk scan), groups
    them by remote parent directory, and uploads each group in one CLI call
    (sequential — bandwidth-bound). Confirmation (the CLI's `info` command,
    one process per file, no batch form) runs on a small thread pool since
    it's I/O wait, not CPU or bandwidth, and dominates wall time once there
    are thousands of files. Manifest mutation, saving, and progress events
    all happen back on the calling thread as each confirm completes — the
    worker threads only ever hash a file and shell out to `info`, never touch
    shared state, so no locking is needed. A failure stops new confirms from
    starting (in-flight ones are allowed to finish) but keeps every
    already-confirmed upload's manifest entry saved.
    """
    error: UploadError | None = None
    uploaded = 0
    members = [
        Member.from_entry(arc, e)
        for arc, e in manifest.files.items()
        if e.get("source") == "sync" and not e.get("uploaded") and Path(e["origin"]).is_file()
    ]
    if not members:
        return uploaded, error
    total = len(members)

    def confirm_one(m: Member) -> tuple[Member, bool]:
        with open(m.origin, "rb") as fh:
            local_sha1 = hashlib.file_digest(fh, "sha1").hexdigest()
        ok = confirm_remote(cfg, cfg.remote_path(m.arcname), m.size, local_sha1)
        return m, ok

    try:
        ensure_remote_folder(cfg)
        groups: dict[str, list[Member]] = {}
        for m in members:
            parent = cfg.remote_path(m.arcname.rsplit("/", 1)[0]) if "/" in m.arcname \
                else cfg.remote_folder
            groups.setdefault(parent, []).append(m)
        ensure_remote_dirs(cfg, manifest, set(groups))
        for parent in sorted(groups):
            group = groups[parent]
            log.info("Uploading %d sync file(s) to %s ...", len(group), parent)
            upload_files(cfg, [m.origin for m in group], parent)

        pool = ThreadPoolExecutor(max_workers=CONFIRM_WORKERS)
        try:
            futures = [pool.submit(confirm_one, m) for group in groups.values() for m in group]
            for future in as_completed(futures):
                m, ok = future.result()
                if not ok:
                    raise UploadError("error", f"remote verification failed for {m.arcname}")
                manifest.files[m.arcname] = m.to_entry_sync(uploaded=now_iso())
                uploaded += 1
                manifest.save()  # persist progress after every file
                if on_progress:
                    on_progress({"event": "sync_file_uploaded", "arcname": m.arcname,
                                "index": uploaded, "total": total})
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    except UploadError as exc:
        error = exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = UploadError("error", str(exc))
    manifest.save()
    return uploaded, error
