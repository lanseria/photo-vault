//
//  RetryPolicy.swift
//  PhotoVaultSync
//
//  失败重试策略（纯逻辑，单测覆盖）。
//  区分三类失败：
//    * 网络/超时/5xx/429：值得退避后再试（局域网抖动、服务器短暂忙）；
//    * 4xx（除 408/429）：请求本身有问题，重试只会重复失败 —— 立刻放弃，交给用户看错误；
//    * 取消：不算失败，不重试。
//  退出当前轮之后仍失败的条目会被记进账本，用户点「重试失败」再跑。
//

import Foundation

/// 失败归类，日志和 UI 提示都靠它
enum UploadFailureKind: String, Sendable, Equatable {
    case network   // 网络层 / 超时 / 5xx / 429：可重试
    case server    // 4xx：不可重试
    case permanent // 参数 / 解码 / 本地读取：不可重试
    case cancelled
}

enum RetryPolicy {
    /// 单个文件最多尝试次数（含第一次）
    static let maxAttempts = 3

    static func kind(of error: Error) -> UploadFailureKind {
        if isCancellation(error) { return .cancelled }
        if let upload = error as? UploadError {
            switch upload {
            case .cancelled:
                return .cancelled
            case .network:
                return .network
            case .server(let status, _):
                return (status == 408 || status == 429 || (500...599).contains(status)) ? .network : .server
            case .invalidResponse:
                return .permanent
            }
        }
        if let photo = error as? PhotoLibraryError {
            switch photo {
            case .exportFailed: return .network   // iCloud 下载失败通常是网络
            case .cancelled: return .cancelled
            case .assetNotFound, .resourceNotFound: return .permanent
            }
        }
        if let server = error as? ServerError {
            return server.isNetworkProblem ? .network : .permanent
        }
        if error is CancellationError { return .cancelled }
        return .permanent
    }

    static func isRetryable(_ error: Error) -> Bool {
        kind(of: error) == .network
    }

    /// 指数退避（秒）：0.5 → 1.5 → 4
    static func delaySeconds(afterAttempt attempt: Int) -> TimeInterval {
        switch attempt {
        case ..<1: return 0
        case 1: return 0.5
        case 2: return 1.5
        default: return 4
        }
    }

    static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let upload = error as? UploadError, case .cancelled = upload { return true }
        if let photo = error as? PhotoLibraryError, case .cancelled = photo { return true }
        if (error as? URLError)?.code == .cancelled { return true }
        return false
    }
}
