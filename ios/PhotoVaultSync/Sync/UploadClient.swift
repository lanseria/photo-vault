//
//  UploadClient.swift
//  PhotoVaultSync
//
//  上传 client：URLSession **background** 配置 + uploadTask(with:fromFile:)。
//  用后台会话的原因：用户切出 App / 锁屏时，上传还能由系统的 nsurlsessiond 继续跑完；
//  回到前台通过 handleEventsForBackgroundURLSession 把完成事件接回来。
//
//  网络细节：
//    * 请求 URL 由 ServerEndpoint.path 拼，带 name/library/batch/taken 四个 query（服务端契约）；
//      taken 是 PHAsset.creationDate（ISO8601 UTC）：文件没有内嵌时间时（聊天保存图、
//      截图重存），服务端用它而不是"上传那一刻"，图库里的日期才对得上；
//    * body 是原始字节（fromFile，不把 GB 级视频读进内存）；
//    * 2xx 一律算成功：后台会话在部分系统版本上不回响应体，此时 duplicate/imported
//      无法区分，但 HTTP 200 已经表示入库成功；响应体能解出来时把 asset id 交给账本；
//    * 非 2xx 用 APIClient.detail 解析 FastAPI 的 {"detail": ...} 给用户看。
//

import Foundation

// MARK: - 错误

enum UploadError: LocalizedError, Equatable {
    case cancelled
    case network(message: String)
    case server(status: Int, detail: String)
    case invalidResponse(String)

    var errorDescription: String? {
        switch self {
        case .cancelled:
            return "已取消"
        case .network(let message):
            return "网络错误：\(message)"
        case .server(let status, let detail):
            return detail.isEmpty ? "服务器返回 HTTP \(status)" : "HTTP \(status)：\(detail)"
        case .invalidResponse(let message):
            return "上传响应异常：\(message)"
        }
    }
}

// MARK: - 协议（便于引擎单测注入假上传器）

@MainActor
protocol UploadTransferring: AnyObject {
    func upload(fileURL: URL, name: String, library: String, batch: String,
                takenAt: Date?, baseURL: URL) async throws -> UploadResponse
    /// 停止当前轮：取消所有在途任务（完成事件会以 .cancelled 回来）
    func cancelAll()
}

// MARK: - 真实实现

final class UploadClient: NSObject, UploadTransferring, @unchecked Sendable {
    static let sessionIdentifier = "com.photovault.ios.upload"
    static let shared = UploadClient()

    private final class Call {
        let continuation: CheckedContinuation<UploadResponse, Error>
        var buffer = Data()
        var task: URLSessionUploadTask?
        init(continuation: CheckedContinuation<UploadResponse, Error>) {
            self.continuation = continuation
        }
    }

    private let lock = NSLock()
    /// taskIdentifier → 在途请求
    private var calls: [Int: Call] = [:]
    /// background session 全部事件处理完后系统给的回调，必须在主线程调用
    private var backgroundCompletionHandler: (() -> Void)?
    private let delegateQueue: OperationQueue
    private var session: URLSession!

    override init() {
        delegateQueue = OperationQueue()
        delegateQueue.maxConcurrentOperationCount = 1
        delegateQueue.name = "com.photovault.ios.upload.delegate"
        super.init()

        // 注意：必须用背景配置 + 固定 identifier，App 重启后同一个 identifier 的
        // 会话能把上次没跑完的任务和完成事件接回来。
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        // 用户手动点的同步要立刻开始，不能被系统「稍后再传」策略拖到没影
        config.isDiscretionary = false
        config.sessionSendsLaunchEvents = true
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 60 * 60
        config.httpMaximumConnectionsPerHost = 4
        if let auth = Self.authHeader() {
            config.httpAdditionalHeaders = ["Authorization": auth]
        }
        session = URLSession(configuration: config, delegate: self, delegateQueue: delegateQueue)
    }

    // MARK: 上传

    @MainActor
    func upload(fileURL: URL, name: String, library: String, batch: String,
                takenAt: Date?, baseURL: URL) async throws -> UploadResponse {
        var query = [
            URLQueryItem(name: "name", value: name),
            URLQueryItem(name: "library", value: library),
            URLQueryItem(name: "batch", value: batch),
        ]
        if let takenAt {
            query.append(URLQueryItem(name: "taken", value: Self.takenQueryValue(takenAt)))
        }
        guard let url = ServerEndpoint.path(baseURL, "api/upload", query: query) else {
            throw UploadError.invalidResponse("上传地址拼装失败")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        return try await withCheckedThrowingContinuation { continuation in
            let task = session.uploadTask(with: request, fromFile: fileURL)
            let call = Call(continuation: continuation)
            call.task = task
            lock.lock()
            calls[task.taskIdentifier] = call
            lock.unlock()
            task.resume()
        }
    }

    @MainActor
    func cancelAll() {
        lock.lock()
        let tasks = calls.values.compactMap(\.task)
        lock.unlock()
        for task in tasks { task.cancel() }
    }

    // MARK: 后台事件接线

    /// AppDelegate.application(_:handleEventsForBackgroundURLSession:completionHandler:)
    func handleBackgroundSessionEvents(identifier: String,
                                       completionHandler: @escaping () -> Void) {
        guard identifier == Self.sessionIdentifier else { return }
        lock.lock()
        backgroundCompletionHandler = completionHandler
        lock.unlock()
    }

    private static func authHeader() -> String? {
        let value = UserDefaults.standard.string(forKey: SettingsKeys.authHeader)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    /// 拍摄时间 → 服务端 query 值（ISO8601 UTC，如 2024-09-01T01:30:00Z）。
    /// PHAsset.creationDate 是绝对瞬时，用 UTC 表达最稳；服务端没有内嵌时间可依据时
    /// 才用它，还墙钟时间用的是服务端默认时区（跟 EXIF 缺 OffsetTime 的处理一致）。
    nonisolated static func takenQueryValue(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter.string(from: date)
    }
}

// MARK: - URLSession 代理

extension UploadClient: URLSessionDataDelegate {

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        calls[dataTask.taskIdentifier]?.buffer.append(data)
        lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let call = calls.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        guard let call else { return }

        if let error {
            if (error as? URLError)?.code == .cancelled {
                call.continuation.resume(throwing: UploadError.cancelled)
            } else {
                call.continuation.resume(throwing: UploadError.network(message: error.localizedDescription))
            }
            return
        }
        guard let http = task.response as? HTTPURLResponse else {
            call.continuation.resume(throwing: UploadError.invalidResponse("响应不是 HTTP"))
            return
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = APIClient.detail(from: call.buffer)
            call.continuation.resume(throwing: UploadError.server(status: http.statusCode, detail: detail))
            return
        }
        // 2xx：能解析就交给上层（拿到 status/asset id），解析不了也按成功处理
        let response = (try? JSONDecoder().decode(UploadResponse.self, from: call.buffer))
            ?? UploadResponse()
        call.continuation.resume(returning: response)
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock()
        let handler = backgroundCompletionHandler
        backgroundCompletionHandler = nil
        lock.unlock()
        guard let handler else { return }
        // 系统要求在主线程调用；不调用会导致 App 被系统杀掉 / 后台任务被判超时
        DispatchQueue.main.async { handler() }
    }
}
