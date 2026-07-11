"""v2 -> v3 manifest migration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backuporganizer.manifest import MANIFEST_VERSION, Manifest

V2_DATA = {
    "version": 2,
    "last_backup": "2026-07-01T00:00:00+00:00",
    "last_upload": "2026-07-01T00:01:00+00:00",
    "next_chunk": 3,
    "chunks": {
        "sync-00001.zip": {"kind": "sync", "size": 100, "sha256": "s1",
                           "files": 2, "uploaded": "2026-07-01T00:00:30+00:00"},
        "sync-00002.zip": {"kind": "sync", "size": 50, "sha256": "s2",
                           "files": 1, "uploaded": ""},  # never uploaded
        "arch-00001.zip": {"kind": "archive", "size": 900, "sha256": "a1",
                           "files": 3, "uploaded": "2026-07-01T00:00:45+00:00"},
    },
    "files": {
        "Sync/Docs/a.md": {"size": 10, "mtime_ns": 1, "sha256": "h1",
                           "source": "sync", "origin": "/tmp/a.md", "chunk": "sync-00001.zip"},
        "Sync/Docs/b.md": {"size": 20, "mtime_ns": 2, "sha256": "h2",
                           "source": "sync", "origin": "/tmp/b.md", "chunk": "sync-00001.zip"},
        "Sync/Docs/c.md": {"size": 5, "mtime_ns": 3, "sha256": "h3",
                           "source": "sync", "origin": "/tmp/c.md", "chunk": "sync-00002.zip"},
        "Archive/photo.jpg": {"size": 900, "mtime_ns": 4, "sha256": "h4",
                              "source": "dropzone", "origin": "/tmp/photo.jpg",
                              "chunk": "arch-00001.zip"},
    },
}


class MigrationTest(unittest.TestCase):
    def test_v2_migrates_to_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(V2_DATA))

            manifest = Manifest.load(path)

            self.assertEqual(MANIFEST_VERSION, 3)
            # Sync files and sync chunks are gone.
            self.assertNotIn("Sync/Docs/a.md", manifest.files)
            self.assertNotIn("Sync/Docs/b.md", manifest.files)
            self.assertNotIn("Sync/Docs/c.md", manifest.files)
            self.assertNotIn("sync-00001.zip", manifest.chunks)
            self.assertNotIn("sync-00002.zip", manifest.chunks)
            # Archive data is untouched.
            self.assertIn("Archive/photo.jpg", manifest.files)
            self.assertIn("arch-00001.zip", manifest.chunks)
            # Only the *uploaded* legacy sync chunk is queued for remote cleanup.
            self.assertEqual(manifest.pending_remote_cleanup, ["sync-00001.zip"])
            self.assertEqual(manifest.deleted_sync, {})
            self.assertEqual(manifest.remote_dirs, [])
            # The old file is backed up untouched, nothing written yet.
            backup = path.with_suffix(".v2.bak.json")
            self.assertTrue(backup.is_file())
            self.assertEqual(json.loads(backup.read_text()), V2_DATA)
            self.assertFalse(path.exists() and json.loads(path.read_text()).get("version") == 3)

    def test_v3_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            manifest = Manifest(path=path)
            manifest.files["Sync/x.txt"] = {
                "size": 1, "mtime_ns": 1, "sha256": "h", "source": "sync",
                "origin": "/tmp/x.txt", "uploaded": "2026-07-01T00:00:00+00:00",
            }
            manifest.deleted_sync["Sync/gone.txt"] = {
                "size": 2, "sha256": "h2", "origin": "/tmp/gone.txt",
                "deleted_at": "2026-07-01T00:00:00+00:00",
            }
            manifest.save()

            reloaded = Manifest.load(path)
            self.assertEqual(reloaded.files, manifest.files)
            self.assertEqual(reloaded.deleted_sync, manifest.deleted_sync)


if __name__ == "__main__":
    unittest.main()
