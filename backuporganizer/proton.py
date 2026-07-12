"""Every interaction with the Proton Drive CLI: upload, download, confirm."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .config import Config
from .manifest import Manifest, Member
from .util import human_size, log, now_iso

SUBPROCESS_TIMEOUT = 15 * 60
TRANSFER_TIMEOUT = 6 * 60 * 60  # multi-GB chunks on a slow uplink take a while

CREATE_FOLDER_RETRIES = 3
CREATE_FOLDER_BACKOFF_SECONDS = 1.5  # doubles each retry: 1.5s, 3s

QUOTA_ERROR_RE = re.compile(r"quota|storage.*(full|exceed)|insufficient|not enough space", re.I)
AUTH_ERROR_RE = re.compile(r"auth|login|session|unauthoriz|forbidden|credential|401|403", re.I)
NOT_FOUND_RE = re.compile(r"not found", re.I)

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


def _parse_list_items(stdout: str) -> list[dict]:
    """Normalizes `filesystem list -j` output into [{name, type, size, sha1}].
    Items whose name couldn't be decrypted (name.ok == False) are skipped —
    there's nothing usable to reconcile them against."""
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    items: list[dict] = []
    for node in raw if isinstance(raw, list) else []:
        name_field = node.get("name", {})
        if not name_field.get("ok") or not name_field.get("value"):
            continue
        rev_field = node.get("activeRevision", {})
        rev = rev_field.get("value", {}) if rev_field.get("ok") else {}
        items.append({
            "name": name_field["value"],
            "type": node.get("type"),
            "size": rev.get("claimedSize", node.get("totalStorageSize", 0)),
            "sha1": rev.get("claimedDigests", {}).get("sha1", ""),
        })
    return items


def remote_list(cfg: Config, path: str) -> list[dict]:
    """One level of `filesystem list -j path`: [] if the path doesn't exist
    or listing otherwise fails (e.g. nothing uploaded there yet)."""
    proc = proton(cfg, "filesystem", "list", "-j", path)
    if proc.returncode != 0:
        return []
    return _parse_list_items(proc.stdout)


def list_remote_tree(cfg: Config, path: str) -> dict[str, dict]:
    """Recursively lists path, returning {full_remote_path: {size, sha1}}
    for every file found below it (folders are walked, not included in the
    result). One `filesystem list` call per folder, not per file."""
    files: dict[str, dict] = {}
    for item in remote_list(cfg, path):
        item_path = f"{path.rstrip('/')}/{item['name']}"
        if item["type"] == "folder":
            files.update(list_remote_tree(cfg, item_path))
        else:
            files[item_path] = {"size": item["size"], "sha1": item["sha1"]}
    return files


def _create_folder(cfg: Config, parent: str, name: str) -> None:
    """create-folder, tolerating 'already exists' and retrying a few times
    with backoff on anything else — a lone transient hiccup (rate limiting,
    a network blip) is expected across a run that can issue thousands of
    these sequentially, and shouldn't abort the whole thing. Raises
    UploadError only once retries are exhausted; a silently-swallowed
    failure would mean every later call needing this folder as a parent
    fails with a confusing "Node not found" instead of the real reason,
    and (if also marked as if it succeeded) never gets retried either.
    """
    output = ""
    for attempt in range(CREATE_FOLDER_RETRIES):
        proc = proton(cfg, "filesystem", "create-folder", parent, name)
        output = proc.stderr + proc.stdout
        if proc.returncode == 0 or "exist" in output.lower():
            return
        if attempt < CREATE_FOLDER_RETRIES - 1:
            log.warning("create-folder %s/%s failed (attempt %d/%d), retrying: %s",
                       parent, name, attempt + 1, CREATE_FOLDER_RETRIES, output.strip()[:200])
            time.sleep(CREATE_FOLDER_BACKOFF_SECONDS * (2 ** attempt))
    raise UploadError(
        classify_upload_error(output),
        f"could not create remote folder {parent.rstrip('/')}/{name} "
        f"after {CREATE_FOLDER_RETRIES} attempts: {output.strip()[:500]}",
    )


def ensure_remote_folder(cfg: Config) -> None:
    """Create each component of remote_folder; 'already exists' is fine.

    The first component is a namespace root (/my-files, ...) that always
    exists and cannot be created, so folder creation starts below it.
    """
    parts = [p for p in cfg.remote_folder.split("/") if p]
    parent = "/" + parts[0]
    for part in parts[1:]:
        _create_folder(cfg, parent, part)
        parent = parent.rstrip("/") + "/" + part


def ensure_remote_dirs(cfg: Config, manifest: Manifest, remote_dirs: set[str]) -> None:
    """Create every ancestor folder of remote_dirs that isn't already known.

    Folders are created top-down (a child's parent must exist first) and
    every newly-created path is recorded in manifest.remote_dirs so later
    runs skip it entirely instead of re-issuing create-folder calls for a
    largely static sync tree. A path that failed to create (see
    _create_folder) is never recorded as known, so a later run retries it.
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
        _create_folder(cfg, parent, name)
        manifest.remote_dirs.append(path)
        known.add(path)


def invalidate_remote_dir(manifest: Manifest, path: str) -> None:
    """Drops path (and anything cached under it) from the remote_dirs cache
    — used when an actual operation discovers the cache was wrong (the
    cloud says the folder isn't there despite being marked known, e.g. from
    a run before the create-folder fix, or someone deleting it by hand on
    Proton Drive). The next ensure_remote_dirs call re-creates it instead
    of continuing to trust a belief the cloud just contradicted."""
    manifest.remote_dirs = [
        d for d in manifest.remote_dirs if d != path and not d.startswith(path + "/")
    ]


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
    be deleted. The manifest's belief that something is uploaded is only
    ever as good as this check — a clear "not found" from the cloud is a
    real negative (the local copy must NOT be deleted, the cloud isn't just
    ambiguous, it's telling us the file isn't there) and is treated as one;
    only a genuinely unreadable/unparseable response (a CLI hiccup, not a
    verdict) is trusted, since the upload itself already exited 0."""
    try:
        proc = proton(cfg, "filesystem", "info", "-j", remote_path)
        if proc.returncode != 0:
            output = proc.stderr + proc.stdout
            if NOT_FOUND_RE.search(output):
                log.error("Remote confirm of %s failed: not found on the cloud", remote_path)
                return False
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
            try:
                upload_files(cfg, [m.origin for m in group], parent)
            except UploadError as exc:
                if not NOT_FOUND_RE.search(str(exc)):
                    raise
                # The manifest believed this folder existed (cached in
                # remote_dirs) but the cloud says otherwise — recreate it
                # and retry once rather than failing the whole run. This is
                # the self-healing case: the manifest is only ever a cache
                # of the cloud's state, not a substitute for it.
                log.warning("Upload to %s failed (%s); recreating the folder and retrying once.",
                           parent, exc)
                invalidate_remote_dir(manifest, parent)
                ensure_remote_dirs(cfg, manifest, {parent})
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
