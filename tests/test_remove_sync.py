"""cmd_remove_sync: stop syncing a folder, orphaning its tracked files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backuporganizer.commands import cmd_remove_sync
from backuporganizer.manifest import Manifest


class RemoveSyncTest(unittest.TestCase):
    def _make_config_file(self, tmp: Path, sync_dirs: list[str]) -> Path:
        config_path = tmp / "config.json"
        config_path.write_text(json.dumps({
            "sync_dirs": sync_dirs,
            "dropzone": str(tmp / "dropzone"),
            "backup_dir": str(tmp / "backup"),
            "manifest": str(tmp / "backup" / "manifest.json"),
            "proton_cli": "/usr/bin/true",
            "remote_folder": "/my-files/Backups/Test",
        }))
        return config_path

    def test_removes_from_config_and_orphans_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs = tmp / "Documents"
            other = tmp / "Other"
            docs.mkdir()
            other.mkdir()
            (tmp / "backup").mkdir()
            config_path = self._make_config_file(tmp, [str(docs), str(other)])

            manifest = Manifest(path=tmp / "backup" / "manifest.json")
            manifest.files["Sync/Documents/a.txt"] = {
                "size": 1, "mtime_ns": 1, "sha256": "x", "source": "sync",
                "origin": str(docs / "a.txt"), "uploaded": "2026-01-01T00:00:00+00:00",
            }
            manifest.files["Sync/Documents/b.txt"] = {
                "size": 1, "mtime_ns": 1, "sha256": "y", "source": "sync",
                "origin": str(docs / "b.txt"), "uploaded": "",
            }
            manifest.files["Sync/Other/c.txt"] = {
                "size": 1, "mtime_ns": 1, "sha256": "z", "source": "sync",
                "origin": str(other / "c.txt"), "uploaded": "2026-01-01T00:00:00+00:00",
            }
            manifest.save()

            rc = cmd_remove_sync(config_path, [docs], json_out=True)
            self.assertEqual(rc, 0)

            raw = json.loads(config_path.read_text())
            self.assertEqual(raw["sync_dirs"], [str(other)])

            manifest = Manifest.load(tmp / "backup" / "manifest.json")
            self.assertNotIn("Sync/Documents/a.txt", manifest.files)
            self.assertNotIn("Sync/Documents/b.txt", manifest.files)
            self.assertIn("Sync/Documents/a.txt", manifest.deleted_sync)
            self.assertIn("Sync/Documents/b.txt", manifest.deleted_sync)
            # Other sync dir untouched
            self.assertIn("Sync/Other/c.txt", manifest.files)
            self.assertNotIn("Sync/Other/c.txt", manifest.deleted_sync)

    def test_not_a_configured_dir_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs = tmp / "Documents"
            docs.mkdir()
            (tmp / "backup").mkdir()
            config_path = self._make_config_file(tmp, [str(docs)])

            rc = cmd_remove_sync(config_path, [tmp / "NeverConfigured"], json_out=True)
            self.assertEqual(rc, 1)
            raw = json.loads(config_path.read_text())
            self.assertEqual(raw["sync_dirs"], [str(docs)])


if __name__ == "__main__":
    unittest.main()
