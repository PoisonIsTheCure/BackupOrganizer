import SwiftUI

/// What's being offered for retirement: one or more archived files whose
/// synced twin (identical content, still in a sync dir) could be dropped
/// now that the archive copy is safely uploaded. retire-sync-twin only
/// needs the archive arcnames — it looks up and re-verifies each twin
/// itself — but the dialog wants readable text to show the user first.
struct RetireSyncPrompt: Identifiable {
    let id = UUID()
    let archiveArcnames: [String]
    let summaryLines: [String]
}

/// Confirmation for retiring a synced copy once its archived twin is
/// confirmed uploaded — deletes the synced file locally (to the Trash) and
/// its cloud copy. Never triggered automatically; this dialog is the only
/// thing that decides to call retire-sync-twin (see docs/CONFIGURATION.md,
/// "Synced folders").
struct ConfirmRetireSyncDialog: ViewModifier {
    @Binding var prompt: RetireSyncPrompt?
    let onConfirm: ([String]) -> Void

    func body(content: Content) -> some View {
        content.confirmationDialog(
            "Remove the synced copy and keep only the archived copy?",
            isPresented: Binding(get: { prompt != nil }, set: { if !$0 { prompt = nil } }),
            presenting: prompt
        ) { prompt in
            Button("Retire Synced Copy", role: .destructive) {
                onConfirm(prompt.archiveArcnames)
            }
            Button("Keep Both", role: .cancel) {}
        } message: { prompt in
            Text(prompt.summaryLines.joined(separator: "\n")
                + "\n\nThe synced copy will move to the Trash locally and be deleted from the cloud. "
                + "The archived (zipped) copy stays and can always be restored.")
        }
    }
}

extension View {
    func confirmRetireSync(prompt: Binding<RetireSyncPrompt?>,
                           onConfirm: @escaping ([String]) -> Void) -> some View {
        modifier(ConfirmRetireSyncDialog(prompt: prompt, onConfirm: onConfirm))
    }
}
