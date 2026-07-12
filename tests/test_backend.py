"""Sync/archive split: find_sync_twins, ensure_remote_dirs caching,
upload_sync_files directory-batching, and the deleted -> deleted_sync
orphan routing in cmd_backup."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backuporganizer import proton as proton_mod
from backuporganizer.commands import cmd_backup
from backuporganizer.config import Config
from backuporganizer.manifest import Manifest, Member
from backuporganizer.scanner import find_sync_twins


def make_config(tmp: Path, **overrides) -> Config:
    raw = {
        "sync_dirs": [str(tmp / "sync")],
        "dropzone": str(tmp / "dropzone"),
        "backup_dir": str(tmp / "backup"),
        "manifest": str(tmp / "backup" / "manifest.json"),
        "proton_cli": "/usr/bin/true",
        "remote_folder": "/my-files/Backups/Test",
        **overrides,
    }
    return Config.from_raw(raw)


def fake_proc(returncode=0, stdout="{}", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FindSyncTwinsTest(unittest.TestCase):
    def test_finds_only_matching_sync_entries(self):
        m = Manifest(path=Path("/tmp/unused-manifest.json"))
        m.files["Archive/photo.jpg"] = {
            "size": 10, "mtime_ns": 1, "sha256": "same", "source": "dropzone",
            "origin": "/tmp/photo.jpg", "chunk": "arch-00001.zip",
        }
        m.files["Sync/Pics/photo.jpg"] = {
            "size": 10, "mtime_ns": 1, "sha256": "same", "source": "sync",
            "origin": "/tmp/sync/photo.jpg", "uploaded": "2026-07-01T00:00:00+00:00",
        }
        m.files["Sync/Pics/other.jpg"] = {
            "size": 20, "mtime_ns": 1, "sha256": "different", "source": "sync",
            "origin": "/tmp/sync/other.jpg", "uploaded": "",
        }
        twins = find_sync_twins(m, "Archive/photo.jpg")
        self.assertEqual([arc for arc, _ in twins], ["Sync/Pics/photo.jpg"])

    def test_unknown_archive_arcname_returns_nothing(self):
        m = Manifest(path=Path("/tmp/unused-manifest.json"))
        self.assertEqual(find_sync_twins(m, "Archive/nope.jpg"), [])


class EnsureRemoteDirsTest(unittest.TestCase):
    def test_second_call_skips_already_created_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            manifest = Manifest(path=Path(tmp) / "manifest.json")
            calls: list[tuple] = []

            def fake(cfg_, *args, **kwargs):
                calls.append(args)
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                proton_mod.ensure_remote_dirs(
                    cfg, manifest, {cfg.remote_path("Sync/Documents/Sub")}
                )
                first_round = len(calls)
                self.assertGreater(first_round, 0)
                self.assertIn(cfg.remote_path("Sync/Documents/Sub"), manifest.remote_dirs)
                self.assertIn(cfg.remote_path("Sync/Documents"), manifest.remote_dirs)

                calls.clear()
                proton_mod.ensure_remote_dirs(
                    cfg, manifest, {cfg.remote_path("Sync/Documents/Sub")}
                )
                self.assertEqual(calls, [])  # nothing new to create

    def test_real_create_folder_failure_raises_and_is_not_marked_known(self):
        # Regression test: a create-folder failure that ISN'T "already
        # exists" used to be silently logged and the path marked known
        # anyway — every later call needing that folder as a parent then
        # failed with a confusing "Node not found" instead of the real
        # reason, and the broken path was never retried on a later run.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            manifest = Manifest(path=Path(tmp) / "manifest.json")
            calls = 0

            def fake(cfg_, *args, **kwargs):
                nonlocal calls
                if args[:2] == ("filesystem", "create-folder") and args[3] == "Documents":
                    calls += 1
                    return fake_proc(returncode=1, stderr="internal server error")
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake), \
                 mock.patch.object(proton_mod.time, "sleep"):
                with self.assertRaises(proton_mod.UploadError) as ctx:
                    proton_mod.ensure_remote_dirs(
                        cfg, manifest, {cfg.remote_path("Sync/Documents/Sub")}
                    )
            self.assertIn("Documents", str(ctx.exception))
            self.assertEqual(calls, proton_mod.CREATE_FOLDER_RETRIES)  # retried, then gave up
            self.assertNotIn(cfg.remote_path("Sync/Documents"), manifest.remote_dirs)
            self.assertNotIn(cfg.remote_path("Sync/Documents/Sub"), manifest.remote_dirs)

    def test_create_folder_succeeds_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            manifest = Manifest(path=Path(tmp) / "manifest.json")
            attempts = 0

            def fake(cfg_, *args, **kwargs):
                nonlocal attempts
                if args[:2] == ("filesystem", "create-folder") and args[3] == "Documents":
                    attempts += 1
                    if attempts < 2:
                        return fake_proc(returncode=1, stderr="temporary hiccup")
                    return fake_proc()
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake), \
                 mock.patch.object(proton_mod.time, "sleep"):
                proton_mod.ensure_remote_dirs(cfg, manifest, {cfg.remote_path("Sync/Documents")})
            self.assertEqual(attempts, 2)
            self.assertIn(cfg.remote_path("Sync/Documents"), manifest.remote_dirs)

    def test_already_exists_failure_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            manifest = Manifest(path=Path(tmp) / "manifest.json")

            def fake(cfg_, *args, **kwargs):
                if args[:2] == ("filesystem", "create-folder"):
                    return fake_proc(returncode=1, stderr="folder already exists")
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                proton_mod.ensure_remote_dirs(
                    cfg, manifest, {cfg.remote_path("Sync/Documents")}
                )
            self.assertIn(cfg.remote_path("Sync/Documents"), manifest.remote_dirs)


class UploadSyncFilesGroupingTest(unittest.TestCase):
    def test_batches_by_remote_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = make_config(tmp)
            manifest = Manifest(path=tmp / "manifest.json")

            files = {
                "Sync/Documents/a.txt": tmp / "a.txt",
                "Sync/Documents/b.txt": tmp / "b.txt",
                "Sync/Documents/Sub/c.txt": tmp / "c.txt",
                "Sync/Other/d.txt": tmp / "d.txt",
            }
            for arc, origin in files.items():
                origin.write_text(arc)
                st = origin.stat()
                member = Member(arc, origin, st.st_size, st.st_mtime_ns, sha256="x")
                manifest.files[arc] = member.to_entry_sync(uploaded="")

            upload_calls: list[tuple] = []

            def fake(cfg_, *args, **kwargs):
                if args[:2] == ("filesystem", "upload"):
                    upload_calls.append(args)
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                uploaded, error = proton_mod.upload_sync_files(cfg, manifest)

            self.assertIsNone(error)
            self.assertEqual(uploaded, 4)
            # 3 distinct remote parent dirs -> 3 upload calls, each batching
            # every file that shares a directory.
            self.assertEqual(len(upload_calls), 3)
            for arc in files:
                self.assertTrue(manifest.files[arc]["uploaded"])

    def test_missing_folder_is_recreated_and_upload_retried(self):
        # Regression test for the "Node not found: Documents" bug: the
        # manifest's remote_dirs cache believed a folder existed (as it
        # would after the old create-folder bug, or if someone deleted it
        # by hand on Proton Drive) but the cloud says otherwise. The upload
        # should self-heal — recreate the folder and retry once — instead
        # of just failing the whole run.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = make_config(tmp)
            manifest = Manifest(path=tmp / "manifest.json")
            manifest.remote_dirs = [cfg.remote_path("Sync"), cfg.remote_path("Sync/Documents")]

            origin = tmp / "a.txt"
            origin.write_text("hello")
            st = origin.stat()
            member = Member("Sync/Documents/a.txt", origin, st.st_size, st.st_mtime_ns, sha256="x")
            manifest.files["Sync/Documents/a.txt"] = member.to_entry_sync(uploaded="")

            upload_attempts = 0
            create_folder_calls: list[tuple] = []

            def fake(cfg_, *args, **kwargs):
                nonlocal upload_attempts
                if args[:2] == ("filesystem", "upload"):
                    upload_attempts += 1
                    if upload_attempts == 1:
                        return fake_proc(returncode=1, stderr="Node not found: Documents")
                    return fake_proc()
                if args[:2] == ("filesystem", "create-folder"):
                    create_folder_calls.append(args)
                return fake_proc()

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                uploaded, error = proton_mod.upload_sync_files(cfg, manifest)

            self.assertIsNone(error)
            self.assertEqual(uploaded, 1)
            self.assertEqual(upload_attempts, 2)  # failed once, retried, succeeded
            # the stale cache entry was dropped and the folder recreated
            self.assertTrue(any(c[3] == "Documents" for c in create_folder_calls))
            self.assertTrue(manifest.files["Sync/Documents/a.txt"]["uploaded"])


class ConfirmRemoteTest(unittest.TestCase):
    def test_not_found_is_a_real_failure_not_a_trusted_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))

            def fake(cfg_, *args, **kwargs):
                return fake_proc(returncode=1, stderr="Node not found: a.txt")

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                ok = proton_mod.confirm_remote(cfg, cfg.remote_path("Sync/a.txt"), 5, "deadbeef")
            self.assertFalse(ok)

    def test_genuinely_ambiguous_failure_is_still_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))

            def fake(cfg_, *args, **kwargs):
                return fake_proc(returncode=1, stderr="temporary CLI hiccup")

            with mock.patch.object(proton_mod, "proton", side_effect=fake):
                ok = proton_mod.confirm_remote(cfg, cfg.remote_path("Sync/a.txt"), 5, "deadbeef")
            self.assertTrue(ok)


class OrphanRoutingTest(unittest.TestCase):
    def test_deleted_sync_file_becomes_orphan_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sync_dir = tmp / "sync"
            sync_dir.mkdir()
            (tmp / "dropzone").mkdir()
            (tmp / "backup").mkdir()
            (sync_dir / "keep.txt").write_text("keep")
            (sync_dir / "gone.txt").write_text("bye")
            cfg = make_config(tmp)

            rc = cmd_backup(cfg, do_upload=False, dry_run=False, notify_enabled=False)
            self.assertEqual(rc, 0)
            manifest = Manifest.load(cfg.manifest_path)
            self.assertIn("Sync/sync/keep.txt", manifest.files)
            self.assertIn("Sync/sync/gone.txt", manifest.files)
            self.assertEqual(manifest.deleted_sync, {})

            (sync_dir / "gone.txt").unlink()
            rc = cmd_backup(cfg, do_upload=False, dry_run=False, notify_enabled=False)
            self.assertEqual(rc, 0)
            manifest = Manifest.load(cfg.manifest_path)
            self.assertNotIn("Sync/sync/gone.txt", manifest.files)
            self.assertIn("Sync/sync/gone.txt", manifest.deleted_sync)
            self.assertIn("Sync/sync/keep.txt", manifest.files)

    def test_reappearing_file_clears_its_orphan_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sync_dir = tmp / "sync"
            sync_dir.mkdir()
            (tmp / "dropzone").mkdir()
            (tmp / "backup").mkdir()
            f = sync_dir / "flaky.txt"
            f.write_text("here")
            cfg = make_config(tmp)

            cmd_backup(cfg, do_upload=False, dry_run=False, notify_enabled=False)
            f.unlink()
            cmd_backup(cfg, do_upload=False, dry_run=False, notify_enabled=False)
            manifest = Manifest.load(cfg.manifest_path)
            self.assertIn("Sync/sync/flaky.txt", manifest.deleted_sync)

            f.write_text("back again")
            cmd_backup(cfg, do_upload=False, dry_run=False, notify_enabled=False)
            manifest = Manifest.load(cfg.manifest_path)
            self.assertNotIn("Sync/sync/flaky.txt", manifest.deleted_sync)
            self.assertIn("Sync/sync/flaky.txt", manifest.files)


if __name__ == "__main__":
    unittest.main()
