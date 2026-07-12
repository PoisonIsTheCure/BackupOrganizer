"""cmd_recalculate_manifest: reconciling the local manifest against a fake
`filesystem list` view of the remote, with no real network calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backuporganizer import proton as proton_mod
from backuporganizer.commands import cmd_recalculate_manifest
from backuporganizer.manifest import Manifest

from .test_backend import fake_proc, make_config


def file_node(name: str, size: int) -> dict:
    return {
        "name": {"ok": True, "value": name},
        "type": "file",
        "activeRevision": {"ok": True, "value": {
            "claimedSize": size, "claimedDigests": {"sha1": "deadbeef"},
        }},
    }


def folder_node(name: str) -> dict:
    return {"name": {"ok": True, "value": name}, "type": "folder"}


class RecalculateManifestTest(unittest.TestCase):
    def test_reconciles_every_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs = tmp / "Documents"
            docs.mkdir()
            (tmp / "backup").mkdir()
            (docs / "recovered.txt").write_bytes(b"aaaaa")   # 5 bytes
            (docs / "confirmed.txt").write_bytes(b"bbbbb")   # 5 bytes
            (docs / "mismatch.txt").write_bytes(b"ccccc")    # 5 bytes locally
            (docs / "stale_but_local.txt").write_bytes(b"ddddd")  # 5 bytes locally
            # "stale_gone_both.txt", "nolocal.txt" and "orphan_still_there.txt"
            # deliberately not created locally

            cfg = make_config(tmp, sync_dirs=[str(docs)])
            manifest = Manifest(path=tmp / "backup" / "manifest.json")

            # confirmed_pending: tracked, marked pending, remote has it (5 bytes), local matches
            manifest.files["Sync/Documents/confirmed.txt"] = {
                "size": 5, "mtime_ns": 1, "sha256": "x", "source": "sync",
                "origin": str(docs / "confirmed.txt"), "uploaded": "",
            }
            # size_mismatch: tracked, marked uploaded, but remote size (999) != recorded size (5)
            manifest.files["Sync/Documents/mismatch.txt"] = {
                "size": 5, "mtime_ns": 1, "sha256": "x", "source": "sync",
                "origin": str(docs / "mismatch.txt"), "uploaded": "2026-01-01T00:00:00+00:00",
            }
            # stale_cleared: tracked, marked uploaded, absent from remote, but the local
            # original still exists -> reset to pending, the next run re-uploads it
            manifest.files["Sync/Documents/stale_but_local.txt"] = {
                "size": 5, "mtime_ns": 1, "sha256": "x", "source": "sync",
                "origin": str(docs / "stale_but_local.txt"), "uploaded": "2026-01-01T00:00:00+00:00",
            }
            # removed_gone_from_both: tracked, marked uploaded, absent from remote AND
            # the local original is also gone -> nothing to preserve, remove outright
            manifest.files["Sync/Documents/stale_gone_both.txt"] = {
                "size": 5, "mtime_ns": 1, "sha256": "x", "source": "sync",
                "origin": str(docs / "stale_gone_both.txt"), "uploaded": "2026-01-01T00:00:00+00:00",
            }
            # orphan still on the remote -> must survive untouched
            manifest.deleted_sync["Sync/Documents/orphan_still_there.txt"] = {
                "size": 5, "sha256": "x", "origin": str(docs / "orphan_still_there.txt"),
                "deleted_at": "2026-01-01T00:00:00+00:00",
            }
            # orphan no longer on the remote -> must be cleared
            manifest.deleted_sync["Sync/Documents/orphan_gone.txt"] = {
                "size": 5, "sha256": "x", "origin": str(docs / "orphan_gone.txt"),
                "deleted_at": "2026-01-01T00:00:00+00:00",
            }
            # archive chunks: one confirmed, one missing/mismatched
            manifest.chunks["arch-00001.zip"] = {
                "kind": "archive", "size": 100, "sha256": "x", "files": 1,
                "uploaded": "2026-01-01T00:00:00+00:00",
            }
            manifest.chunks["arch-00002.zip"] = {
                "kind": "archive", "size": 999, "sha256": "x", "files": 1,
                "uploaded": "2026-01-01T00:00:00+00:00",
            }
            manifest.save()

            sync_root = cfg.remote_path("Sync")
            docs_root = cfg.remote_path("Sync/Documents")

            def fake(cfg_, *args, **kwargs):
                if args[:3] == ("filesystem", "list", "-j"):
                    path = args[3]
                    if path == sync_root:
                        return fake_proc(stdout=json.dumps([folder_node("Documents")]))
                    if path == docs_root:
                        return fake_proc(stdout=json.dumps([
                            file_node("recovered.txt", 5),
                            file_node("confirmed.txt", 5),
                            file_node("mismatch.txt", 999),
                            file_node("nolocal.txt", 5),
                            file_node("orphan_still_there.txt", 5),
                        ]))
                    if path == cfg_.remote_folder:
                        return fake_proc(stdout=json.dumps([
                            file_node("arch-00001.zip", 100),
                            file_node("arch-00002.zip", 12345),  # mismatched vs manifest's 999
                        ]))
                    return fake_proc(returncode=1)
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                rc = cmd_recalculate_manifest(cfg, json_out=True)
            self.assertEqual(rc, 0)

            manifest = Manifest.load(cfg.manifest_path)

            # newly_recovered
            self.assertIn("Sync/Documents/recovered.txt", manifest.files)
            self.assertTrue(manifest.files["Sync/Documents/recovered.txt"]["uploaded"])

            # confirmed_pending
            self.assertTrue(manifest.files["Sync/Documents/confirmed.txt"]["uploaded"])

            # size_mismatch: re-recorded from the local file (still 5, since local didn't change)
            self.assertTrue(manifest.files["Sync/Documents/mismatch.txt"]["uploaded"])
            self.assertEqual(manifest.files["Sync/Documents/mismatch.txt"]["size"], 5)

            # stale_cleared: reset to pending, not deleted outright (local original exists)
            self.assertIn("Sync/Documents/stale_but_local.txt", manifest.files)
            self.assertEqual(manifest.files["Sync/Documents/stale_but_local.txt"]["uploaded"], "")

            # removed_gone_from_both: fully removed, not left dangling as "pending forever"
            self.assertNotIn("Sync/Documents/stale_gone_both.txt", manifest.files)

            # orphans
            self.assertIn("Sync/Documents/orphan_still_there.txt", manifest.deleted_sync)
            self.assertNotIn("Sync/Documents/orphan_gone.txt", manifest.deleted_sync)

            # nolocal.txt: found on remote, no local file -> not added to manifest.files
            self.assertNotIn("Sync/Documents/nolocal.txt", manifest.files)

            # archive: chunk 1 confirmed (untouched), chunk 2 reset to pending
            self.assertEqual(manifest.chunks["arch-00001.zip"]["uploaded"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(manifest.chunks["arch-00002.zip"]["uploaded"], "")


if __name__ == "__main__":
    unittest.main()
