import AppKit

/// Thin NSOpenPanel wrapper. SwiftUI's `.fileImporter` can't mix "choose
/// files or folders in the same pick" (needed for Archive), so both pickers
/// go through AppKit directly instead of splitting into two importers.
enum FilePicker {
    @MainActor
    static func choose(canChooseFiles: Bool, canChooseDirectories: Bool,
                       prompt: String) -> [String] {
        let panel = NSOpenPanel()
        panel.canChooseFiles = canChooseFiles
        panel.canChooseDirectories = canChooseDirectories
        panel.allowsMultipleSelection = true
        panel.prompt = prompt
        panel.canCreateDirectories = false
        guard panel.runModal() == .OK else { return [] }
        return panel.urls.map(\.path)
    }
}
