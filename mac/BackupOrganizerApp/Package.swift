// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "BackupOrganizerApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "BackupOrganizerApp",
            path: "Sources/BackupOrganizerApp"
        ),
        .testTarget(
            name: "BackupOrganizerAppTests",
            dependencies: ["BackupOrganizerApp"],
            path: "Tests/BackupOrganizerAppTests"
        ),
    ]
)
