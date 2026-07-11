import XCTest
@testable import BackupOrganizerApp

final class FileTreeTests: XCTestCase {
    private func entry(_ arcname: String, size: Int = 10) -> FileEntry {
        FileEntry(arcname: arcname, size: size, sha256: "h", source: "sync", uploaded: "")
    }

    func testFoldsFlatArcnamesIntoNestedFolders() {
        let entries = [
            entry("Sync/Documents/notes.md"),
            entry("Sync/Documents/Sub/deep.txt"),
            entry("Sync/Photos/a.jpg"),
        ]
        let root = buildFileTree(entries)

        XCTAssertEqual(root.sortedFiles.count, 0)
        XCTAssertEqual(root.sortedDirs.map(\.name), ["Sync"])

        let sync = root.sortedDirs[0]
        XCTAssertEqual(sync.sortedDirs.map(\.name), ["Documents", "Photos"])
        XCTAssertEqual(sync.count, 3)
        XCTAssertEqual(sync.size, 30)

        let documents = sync.sortedDirs[0]
        XCTAssertEqual(documents.sortedFiles.map(\.arcname), ["Sync/Documents/notes.md"])
        XCTAssertEqual(documents.sortedDirs.map(\.name), ["Sub"])

        let sub = documents.sortedDirs[0]
        XCTAssertEqual(sub.sortedFiles.map(\.arcname), ["Sync/Documents/Sub/deep.txt"])
    }

    func testEmptyListProducesEmptyRoot() {
        let root = buildFileTree([])
        XCTAssertTrue(root.sortedDirs.isEmpty)
        XCTAssertTrue(root.sortedFiles.isEmpty)
    }
}
