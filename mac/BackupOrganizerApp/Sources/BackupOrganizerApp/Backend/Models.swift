import Foundation

/// Mirrors `--status --json` in backuporganizer/commands.py.
struct StatusInfo: Codable, Equatable {
    var filesTotal: Int
    var syncCount: Int
    var archiveCount: Int
    var totalBytes: Int
    var chunksTotal: Int
    var chunksPending: [String]
    var syncPending: Int
    var orphansPending: Int
    var localCacheBytes: Int
    var lastBackup: String
    var lastUpload: String
    var dropzonePending: Int
    var remoteFolder: String
    var logPath: String?

    enum CodingKeys: String, CodingKey {
        case filesTotal = "files_total"
        case syncCount = "sync_count"
        case archiveCount = "archive_count"
        case totalBytes = "total_bytes"
        case chunksTotal = "chunks_total"
        case chunksPending = "chunks_pending"
        case syncPending = "sync_pending"
        case orphansPending = "orphans_pending"
        case localCacheBytes = "local_cache_bytes"
        case lastBackup = "last_backup"
        case lastUpload = "last_upload"
        case dropzonePending = "dropzone_pending"
        case remoteFolder = "remote_folder"
        case logPath = "log_path"
    }
}

/// One row from `--list {sync,archive,all} --json`. Sync and archive rows
/// share this shape; archive-only fields (chunk/relocatable/twinConfirmed)
/// are nil on sync rows. "uploaded" is always an ISO timestamp string or ""
/// for both kinds — never a bool — matching the Python side's contract.
struct FileEntry: Codable, Equatable, Identifiable {
    var arcname: String
    var size: Int
    var sha256: String
    var source: String // "sync" | "dropzone"
    var uploaded: String
    var chunk: String?
    var relocatable: Bool?
    var twinConfirmed: Bool?

    var id: String { arcname }
    var isUploaded: Bool { !uploaded.isEmpty }
    var isSynced: Bool { source == "sync" }

    enum CodingKeys: String, CodingKey {
        case arcname, size, sha256, source, uploaded, chunk, relocatable
        case twinConfirmed = "twin_confirmed"
    }
}

/// One row from `--orphans --json`: a synced file deleted locally but still
/// on the cloud, awaiting an explicit delete-remote.
struct OrphanEntry: Codable, Equatable, Identifiable {
    var arcname: String
    var size: Int
    var sha256: String
    var origin: String
    var deletedAt: String

    var id: String { arcname }

    enum CodingKeys: String, CodingKey {
        case arcname, size, sha256, origin
        case deletedAt = "deleted_at"
    }
}

/// One entry in `archive PATH... --json`'s "relocatable" list: an archived
/// file that still has a live sync-dir twin worth offering to retire.
struct RelocatableTwin: Codable, Equatable, Identifiable {
    var archiveArcname: String
    var syncArcname: String
    var twinConfirmed: Bool

    var id: String { archiveArcname + "|" + syncArcname }

    enum CodingKeys: String, CodingKey {
        case archiveArcname = "archive_arcname"
        case syncArcname = "sync_arcname"
        case twinConfirmed = "twin_confirmed"
    }
}

/// Result of `archive PATH... --json`.
struct ArchiveResult: Codable, Equatable {
    var moved: [String]
    var skipped: [String]
    var stagedOnly: Bool?
    var backupExitCode: Int?
    var relocatable: [RelocatableTwin]

    enum CodingKeys: String, CodingKey {
        case moved, skipped, relocatable
        case stagedOnly = "staged_only"
        case backupExitCode = "backup_exit_code"
    }
}

/// Result of `delete-remote ARCNAME... --json`.
struct DeleteRemoteResult: Codable, Equatable {
    var deleted: [String]
    var notFound: [String]
    var errors: [String: String]

    enum CodingKeys: String, CodingKey {
        case deleted, errors
        case notFound = "not_found"
    }
}

/// One entry in `retire-sync-twin ARC... --json`'s "skipped" list.
struct RetireSkip: Codable, Equatable {
    var archiveArcname: String
    var syncArcname: String?
    var reason: String

    enum CodingKeys: String, CodingKey {
        case archiveArcname = "archive_arcname"
        case syncArcname = "sync_arcname"
        case reason
    }
}

/// One entry in `retire-sync-twin ARC... --json`'s "retired" list.
struct RetiredTwin: Codable, Equatable {
    var archiveArcname: String
    var syncArcname: String

    enum CodingKeys: String, CodingKey {
        case archiveArcname = "archive_arcname"
        case syncArcname = "sync_arcname"
    }
}

/// Result of `retire-sync-twin ARC... --json`.
struct RetireSyncTwinResult: Codable, Equatable {
    var retired: [RetiredTwin]
    var skipped: [RetireSkip]
}

/// One NDJSON line from `run --json` (see cmd_backup in commands.py for the
/// exact event list). Every field beyond `event` is optional because each
/// event kind only populates a subset — this mirrors the Python side's
/// per-event dict shapes rather than forcing one rigid schema.
struct ProgressEvent: Codable, Equatable {
    var event: String

    // diff
    var added: Int?
    var changed: Int?
    var deleted: Int?
    var touched: Int?
    var dropzone: Int?

    // chunk_build
    var name: String?
    var index: Int?
    var total: Int?
    var files: Int?

    // sync_upload_start / archive_upload_start
    var count: Int?

    // sync_file_uploaded / chunk_uploaded share name/index/total above;
    // sync_file_uploaded also has:
    var arcname: String?

    // dropzone_trashed uses `name` above.

    // result
    var ok: Bool?
    var error: String?
    var errorKind: String?
    var syncUploaded: Int?
    var archiveUploaded: Int?
    var freedBytes: Int?
    var trashed: Int?
    var waiting: Int?

    enum CodingKeys: String, CodingKey {
        case event, added, changed, deleted, touched, dropzone, name, index, total,
             files, count, arcname, ok, error, trashed, waiting
        case errorKind = "error_kind"
        case syncUploaded = "sync_uploaded"
        case archiveUploaded = "archive_uploaded"
        case freedBytes = "freed_bytes"
    }

    var isTerminal: Bool { event == "result" }
}

/// Result of `--log-tail N --json`: the tail of backup_organizer.log.
struct LogTail: Codable, Equatable {
    var path: String
    var lines: [String]
}

/// Result of `add-sync DIR... --json`.
struct AddSyncResult: Codable, Equatable {
    var added: [String]
    var skipped: [String]
    var stagedOnly: Bool?
    var backupExitCode: Int?

    enum CodingKeys: String, CodingKey {
        case added, skipped
        case stagedOnly = "staged_only"
        case backupExitCode = "backup_exit_code"
    }
}
