import Foundation

/// Simple async-load state shared by every read-only view — avoids each
/// view re-inventing loading/error/empty handling.
enum LoadState<T> {
    case loading
    case loaded(T)
    case failed(String)
}
