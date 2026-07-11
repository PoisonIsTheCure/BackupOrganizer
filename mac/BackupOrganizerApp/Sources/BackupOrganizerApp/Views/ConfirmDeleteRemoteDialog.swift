import SwiftUI

/// Confirmation for permanently deleting an orphaned synced file's cloud
/// copy — the one place in this app that destroys data with no Trash
/// safety net (delete-remote calls `filesystem trash` on Proton Drive
/// directly), so it gets its own explicit, named confirmation dialog.
struct ConfirmDeleteRemoteDialog: ViewModifier {
    @Binding var orphan: OrphanEntry?
    let onConfirm: (OrphanEntry) -> Void

    func body(content: Content) -> some View {
        content.confirmationDialog(
            "Permanently delete this file from the cloud?",
            isPresented: Binding(get: { orphan != nil }, set: { if !$0 { orphan = nil } }),
            presenting: orphan
        ) { orphan in
            Button("Delete from Cloud", role: .destructive) {
                onConfirm(orphan)
            }
            Button("Cancel", role: .cancel) {}
        } message: { orphan in
            Text("\(orphan.arcname)\n\nThis file was already removed locally on \(orphan.deletedAt). "
                + "Deleting it from the cloud cannot be undone — this is the only remaining copy.")
        }
    }
}

extension View {
    func confirmDeleteRemote(orphan: Binding<OrphanEntry?>,
                             onConfirm: @escaping (OrphanEntry) -> Void) -> some View {
        modifier(ConfirmDeleteRemoteDialog(orphan: orphan, onConfirm: onConfirm))
    }
}
