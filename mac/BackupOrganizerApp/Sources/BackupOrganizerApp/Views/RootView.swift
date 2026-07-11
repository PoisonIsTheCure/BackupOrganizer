import SwiftUI

enum SidebarItem: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case synced = "Synced"
    case archived = "Archived"

    var id: String { rawValue }

    var systemImage: String {
        switch self {
        case .dashboard: return "gauge"
        case .synced: return "arrow.triangle.2.circlepath"
        case .archived: return "archivebox"
        }
    }
}

struct RootView: View {
    @State private var selection: SidebarItem? = .dashboard
    private let client = BackendClient(settings: .shared)

    var body: some View {
        NavigationSplitView {
            List(SidebarItem.allCases, selection: $selection) { item in
                Label(item.rawValue, systemImage: item.systemImage).tag(item)
            }
            .navigationTitle("BackupOrganizer")
        } detail: {
            switch selection ?? .dashboard {
            case .dashboard: DashboardView(client: client)
            case .synced: SyncedView(client: client)
            case .archived: ArchivedView(client: client)
            }
        }
        .frame(minWidth: 760, minHeight: 480)
    }
}
