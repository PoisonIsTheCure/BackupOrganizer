import Foundation

/// Byte formatting to match backuporganizer/util.py's human_size (same
/// units, same one-decimal style) so numbers read identically in the CLI
/// and the app.
func humanSize(_ bytes: Int) -> String {
    var n = Double(bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"] {
        if n < 1024 || unit == "TB" {
            return unit == "B" ? "\(Int(n)) B" : String(format: "%.1f %@", n, unit)
        }
        n /= 1024
    }
    return String(format: "%.1f TB", n)
}
