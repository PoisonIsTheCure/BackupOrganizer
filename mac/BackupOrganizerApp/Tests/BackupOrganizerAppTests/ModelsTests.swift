import XCTest
@testable import BackupOrganizerApp

/// Decodes fixture JSON strings shaped exactly like backuporganizer's
/// --json output through the Codable models — no live subprocess, no CLI
/// dependency, just verifying the Swift <-> Python JSON contract holds.
final class ModelsTests: XCTestCase {
    func testDecodeStatusInfo() throws {
        let json = """
        {"files_total": 42, "sync_count": 30, "archive_count": 12,
         "total_bytes": 123456, "chunks_total": 3,
         "chunks_pending": ["arch-00003.zip"], "sync_pending": 2,
         "orphans_pending": 1, "local_cache_bytes": 0,
         "last_backup": "2026-07-11T21:00:00+02:00",
         "last_upload": "2026-07-11T21:02:00+02:00",
         "dropzone_pending": 0, "remote_folder": "/my-files/Backups/MacBookAir"}
        """
        let status = try JSONDecoder().decode(StatusInfo.self, from: Data(json.utf8))
        XCTAssertEqual(status.filesTotal, 42)
        XCTAssertEqual(status.syncCount, 30)
        XCTAssertEqual(status.chunksPending, ["arch-00003.zip"])
        XCTAssertEqual(status.remoteFolder, "/my-files/Backups/MacBookAir")
    }

    func testDecodeSyncFileEntryHasNoArchiveOnlyFields() throws {
        let json = """
        {"arcname": "Sync/Documents/notes.md", "size": 1234, "sha256": "abc",
         "source": "sync", "uploaded": "2026-07-11T21:00:00+02:00"}
        """
        let entry = try JSONDecoder().decode(FileEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.arcname, "Sync/Documents/notes.md")
        XCTAssertTrue(entry.isSynced)
        XCTAssertTrue(entry.isUploaded)
        XCTAssertNil(entry.chunk)
        XCTAssertNil(entry.relocatable)
    }

    func testDecodePendingSyncFileEntry() throws {
        let json = """
        {"arcname": "Sync/Documents/draft.md", "size": 1, "sha256": "x",
         "source": "sync", "uploaded": ""}
        """
        let entry = try JSONDecoder().decode(FileEntry.self, from: Data(json.utf8))
        XCTAssertFalse(entry.isUploaded)
    }

    func testDecodeArchiveFileEntryWithRelocatableTwin() throws {
        let json = """
        {"arcname": "Archive/Photos/vacation.jpg", "size": 812233, "sha256": "def",
         "source": "dropzone", "uploaded": "2026-07-03T21:01:12+02:00",
         "chunk": "arch-00002.zip", "relocatable": true, "twin_confirmed": true}
        """
        let entry = try JSONDecoder().decode(FileEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.chunk, "arch-00002.zip")
        XCTAssertEqual(entry.relocatable, true)
        XCTAssertEqual(entry.twinConfirmed, true)
        XCTAssertTrue(entry.isUploaded)
        XCTAssertFalse(entry.isSynced)
    }

    func testDecodeOrphanEntry() throws {
        let json = """
        {"arcname": "Sync/Documents/old_notes.md", "size": 4096, "sha256": "ghi",
         "origin": "/Users/you/Documents/old_notes.md",
         "deleted_at": "2026-07-10T09:00:00+02:00"}
        """
        let orphan = try JSONDecoder().decode(OrphanEntry.self, from: Data(json.utf8))
        XCTAssertEqual(orphan.deletedAt, "2026-07-10T09:00:00+02:00")
        XCTAssertEqual(orphan.id, orphan.arcname)
    }

    func testDecodeArchiveResultWithRelocatable() throws {
        let json = """
        {"moved": ["/tmp/dropzone/report.pdf"], "skipped": [], "staged_only": false,
         "backup_exit_code": 0,
         "relocatable": [{"archive_arcname": "Archive/report.pdf",
                          "sync_arcname": "Sync/Documents/report.pdf",
                          "twin_confirmed": true}]}
        """
        let result = try JSONDecoder().decode(ArchiveResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.relocatable.count, 1)
        XCTAssertEqual(result.relocatable[0].syncArcname, "Sync/Documents/report.pdf")
        XCTAssertEqual(result.backupExitCode, 0)
    }

    func testDecodeDeleteRemoteResult() throws {
        let json = """
        {"deleted": ["Sync/a.txt"], "not_found": ["Sync/b.txt"],
         "errors": {"Sync/c.txt": "network error"}}
        """
        let result = try JSONDecoder().decode(DeleteRemoteResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.deleted, ["Sync/a.txt"])
        XCTAssertEqual(result.notFound, ["Sync/b.txt"])
        XCTAssertEqual(result.errors["Sync/c.txt"], "network error")
    }

    func testDecodeRetireSyncTwinResult() throws {
        let json = """
        {"retired": [{"archive_arcname": "Archive/a.jpg", "sync_arcname": "Sync/Pics/a.jpg"}],
         "skipped": [{"archive_arcname": "Archive/b.jpg", "sync_arcname": "Sync/Pics/b.jpg",
                      "reason": "sync copy changed since archiving"}]}
        """
        let result = try JSONDecoder().decode(RetireSyncTwinResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.retired.count, 1)
        XCTAssertEqual(result.skipped.first?.reason, "sync copy changed since archiving")
    }

    func testDecodeRecalculateManifestResult() throws {
        let json = """
        {"sync": {"remote_sync_files": 5, "newly_recovered": ["Sync/a.txt"],
                  "confirmed_pending": ["Sync/b.txt"], "size_mismatch": [],
                  "unmatched_no_local": ["Sync/c.txt"], "stale_cleared": ["Sync/d.txt"],
                  "orphans_cleared": ["Sync/e.txt"]},
         "archive": {"confirmed": ["arch-00001.zip"], "reset_to_pending": ["arch-00002.zip"]}}
        """
        let result = try JSONDecoder().decode(RecalculateManifestResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.sync.remoteSyncFiles, 5)
        XCTAssertEqual(result.sync.newlyRecovered, ["Sync/a.txt"])
        XCTAssertEqual(result.sync.unmatchedNoLocal, ["Sync/c.txt"])
        XCTAssertEqual(result.archive.confirmed, ["arch-00001.zip"])
        XCTAssertEqual(result.archive.resetToPending, ["arch-00002.zip"])
    }

    /// One NDJSON line per event kind cmd_backup's json_out path can emit
    /// (see commands.py's cmd_backup) — every line must decode and every
    /// event-specific field must round-trip.
    func testDecodeEveryProgressEventKind() throws {
        let lines: [(String, (ProgressEvent) -> Void)] = [
            (#"{"event": "diff", "added": 2, "changed": 1, "deleted": 0, "touched": 3, "dropzone": 1}"#,
             { XCTAssertEqual($0.added, 2); XCTAssertEqual($0.dropzone, 1) }),
            (#"{"event": "chunk_build", "name": "arch-00001.zip", "index": 1, "total": 2, "files": 3}"#,
             { XCTAssertEqual($0.name, "arch-00001.zip"); XCTAssertEqual($0.files, 3) }),
            (#"{"event": "sync_upload_start", "count": 5}"#,
             { XCTAssertEqual($0.count, 5) }),
            (#"{"event": "sync_file_uploaded", "arcname": "Sync/a.txt", "index": 1, "total": 5}"#,
             { XCTAssertEqual($0.arcname, "Sync/a.txt") }),
            (#"{"event": "archive_upload_start"}"#, { _ in }),
            (#"{"event": "chunk_uploaded", "name": "arch-00001.zip", "index": 1, "total": 1}"#,
             { XCTAssertEqual($0.name, "arch-00001.zip") }),
            (#"{"event": "dropzone_trashed", "name": "report.pdf"}"#,
             { XCTAssertEqual($0.name, "report.pdf") }),
            (#"{"event": "result", "ok": true, "error": null, "added": 2, "changed": 1, "deleted": 0, "sync_uploaded": 5, "archive_uploaded": 2, "freed_bytes": 1024, "trashed": 1, "waiting": 0}"#,
             { XCTAssertEqual($0.ok, true); XCTAssertTrue($0.isTerminal); XCTAssertEqual($0.freedBytes, 1024) }),
            (#"{"event": "result", "ok": false, "error": "quota exceeded", "error_kind": "quota"}"#,
             { XCTAssertEqual($0.ok, false); XCTAssertEqual($0.errorKind, "quota"); XCTAssertTrue($0.isTerminal) }),
        ]
        for (json, check) in lines {
            let event = try JSONDecoder().decode(ProgressEvent.self, from: Data(json.utf8))
            check(event)
        }
    }

    func testDecodeLogTail() throws {
        let json = """
        {"path": "/Users/you/Backups/BackupOrganizer/backup_organizer.log",
         "lines": ["2026-07-11 20:02:41,449 INFO    Diff: 3797 added, 0 changed"]}
        """
        let tail = try JSONDecoder().decode(LogTail.self, from: Data(json.utf8))
        XCTAssertTrue(tail.path.hasSuffix("backup_organizer.log"))
        XCTAssertEqual(tail.lines.count, 1)
    }

    func testDecodeStatusInfoWithLogPath() throws {
        let json = """
        {"files_total": 0, "sync_count": 0, "archive_count": 0, "total_bytes": 0,
         "chunks_total": 0, "chunks_pending": [], "sync_pending": 0, "orphans_pending": 0,
         "local_cache_bytes": 0, "last_backup": "", "last_upload": "", "dropzone_pending": 0,
         "remote_folder": "/my-files/x", "log_path": "/Users/you/Backups/BackupOrganizer/backup_organizer.log"}
        """
        let status = try JSONDecoder().decode(StatusInfo.self, from: Data(json.utf8))
        XCTAssertEqual(status.logPath, "/Users/you/Backups/BackupOrganizer/backup_organizer.log")
    }
}
