import SwiftUI

/// One folder in the tree built from a flat arcname list. A class (not a
/// struct) because building it is naturally mutative — a dictionary lookup
/// per path segment while walking every entry once — and it's only ever
/// read afterward by the view.
final class FileTreeNode: Identifiable {
    let id: String
    let name: String
    fileprivate var dirsByName: [String: FileTreeNode] = [:]
    var files: [FileEntry] = []
    var size = 0
    var count = 0

    init(id: String, name: String) {
        self.id = id
        self.name = name
    }

    var sortedDirs: [FileTreeNode] {
        dirsByName.values.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    var sortedFiles: [FileEntry] {
        files.sorted { $0.arcname.localizedStandardCompare($1.arcname) == .orderedAscending }
    }
}

/// Folds a flat arcname list ("Sync/Documents/notes.md") into a folder
/// tree, mirroring viewer.py's build_tree (the same structure `--tree`
/// prints in the terminal) so the app's folder view matches the CLI's.
func buildFileTree(_ entries: [FileEntry]) -> FileTreeNode {
    let root = FileTreeNode(id: "", name: "")
    for entry in entries {
        let parts = entry.arcname.split(separator: "/").map(String.init)
        guard !parts.isEmpty else { continue }
        var node = root
        var path = ""
        for part in parts.dropLast() {
            path = path.isEmpty ? part : "\(path)/\(part)"
            if let existing = node.dirsByName[part] {
                node = existing
            } else {
                let child = FileTreeNode(id: path, name: part)
                node.dirsByName[part] = child
                node = child
            }
            node.size += entry.size
            node.count += 1
        }
        node.files.append(entry)
    }
    return root
}

/// Renders a FileTreeNode as collapsible folders down to file leaves.
/// `trailing` customizes the per-file accessory (upload badge, retire
/// button, ...) since Synced and Archived want different ones.
struct FileTreeView<Trailing: View>: View {
    let root: FileTreeNode
    @ViewBuilder let trailing: (FileEntry) -> Trailing

    var body: some View {
        ForEach(root.sortedDirs) { dir in
            FileTreeFolderRow(node: dir, trailing: trailing)
        }
        ForEach(root.sortedFiles) { file in
            FileTreeFileRow(file: file, trailing: trailing)
        }
    }
}

private struct FileTreeFolderRow<Trailing: View>: View {
    let node: FileTreeNode
    @ViewBuilder let trailing: (FileEntry) -> Trailing
    @State private var expanded = true

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            ForEach(node.sortedDirs) { child in
                FileTreeFolderRow(node: child, trailing: trailing)
            }
            ForEach(node.sortedFiles) { file in
                FileTreeFileRow(file: file, trailing: trailing)
            }
        } label: {
            HStack {
                Image(systemName: "folder.fill").foregroundStyle(.secondary)
                Text(node.name)
                Spacer()
                Text("\(node.count) file\(node.count == 1 ? "" : "s") · \(humanSize(node.size))")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct FileTreeFileRow<Trailing: View>: View {
    let file: FileEntry
    @ViewBuilder let trailing: (FileEntry) -> Trailing

    private var leafName: String {
        file.arcname.split(separator: "/").last.map(String.init) ?? file.arcname
    }

    var body: some View {
        HStack {
            Image(systemName: "doc").foregroundStyle(.secondary)
            Text(leafName).font(.body.monospaced())
            Spacer()
            Text(humanSize(file.size)).font(.caption).foregroundStyle(.secondary)
            trailing(file)
        }
        .padding(.leading, 4)
    }
}
