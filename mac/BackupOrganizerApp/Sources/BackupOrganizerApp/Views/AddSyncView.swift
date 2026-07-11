import SwiftUI

/// "Add Sync Folder…" button: picks folder(s) via NSOpenPanel, then calls
/// add-sync (which validates and runs the backup cycle). Single files are
/// rejected by the backend — sync entries must be folders.
struct AddSyncButton: View {
    let client: BackendClient
    let onCompleted: () -> Void

    @State private var isRunning = false
    @State private var error: String?

    var body: some View {
        Button {
            let dirs = FilePicker.choose(canChooseFiles: false, canChooseDirectories: true,
                                         prompt: "Add to Sync")
            guard !dirs.isEmpty else { return }
            Task { await addSync(dirs) }
        } label: {
            Label("Add Sync Folder…", systemImage: "folder.badge.plus")
        }
        .disabled(isRunning)
        .alert("Couldn't add sync folder", isPresented: Binding(
            get: { error != nil }, set: { if !$0 { error = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(error ?? "")
        }
    }

    private func addSync(_ dirs: [String]) async {
        isRunning = true
        defer { isRunning = false }
        do {
            let result = try await client.addSync(dirs: dirs)
            if result.added.isEmpty && !result.skipped.isEmpty {
                error = "Already syncing: \(result.skipped.joined(separator: ", "))"
            }
            onCompleted()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
