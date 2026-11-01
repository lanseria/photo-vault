//
//  APIClient.swift
//  PhotoVaultSync
//
//  只读接口（统计 / 列表）与 URL 拼装。上传走 Sync/UploadClient（后台 URLSession）。
//  所有请求都带超时和可读错误，UI 可以原样展示 errorDescription。
//

import Foundation

final class APIClient: @unchecked Sendable {
    let baseURL: URL
    private let session: URLSession

    init(baseURL: URL, timeout: TimeInterval = 10, session: URLSession? = nil) {
        self.baseURL = baseURL
        if let session {
            self.session = session
            return
        }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = timeout
        cfg.timeoutIntervalForResource = max(60, timeout * 6)
        cfg.waitsForConnectivity = false
        cfg.httpMaximumConnectionsPerHost = 4
        let auth = UserDefaults.standard.string(forKey: SettingsKeys.authHeader)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let auth, !auth.isEmpty {
            // 可选：放在反代后面的 Basic Auth（值为完整的 Authorization 头）
            cfg.httpAdditionalHeaders = ["Authorization": auth]
        }
        self.session = URLSession(configuration: cfg)
    }

    // MARK: - 接口

    /// GET /api/stats
    func stats() async throws -> RemoteStats {
        try await get(path: "api/stats")
    }

    /// GET /api/assets?limit=&offset=&library=&kind=
    func assets(limit: Int = 120, offset: Int = 0,
                library: String = "gallery", kind: String? = nil) async throws -> AssetPage {
        var query = [
            URLQueryItem(name: "limit", value: String(max(1, min(limit, 300)))),
            URLQueryItem(name: "offset", value: String(max(0, offset))),
            URLQueryItem(name: "library", value: library),
        ]
        if let kind, !kind.isEmpty {
            query.append(URLQueryItem(name: "kind", value: kind))
        }
        return try await get(path: "api/assets", query: query)
    }

    // MARK: - 静态资源 URL

    func thumbURL(sha: String) -> URL? {
        guard sha.count == 64 else { return nil }
        return ServerEndpoint.path(baseURL, "thumbs/\(sha).webp")
    }

    func previewURL(assetID: String) -> URL? {
        ServerEndpoint.path(baseURL, "preview/\(assetID)")
    }

    /// 原件；role=live_motion 是 Live Photo 的动态部分
    func fileURL(assetID: String, role: String = "original") -> URL? {
        var query: [URLQueryItem] = []
        if role != "original" {
            query.append(URLQueryItem(name: "role", value: role))
        }
        return ServerEndpoint.path(baseURL, "file/\(assetID)", query: query)
    }

    func trashURL(assetID: String) -> URL? {
        ServerEndpoint.path(baseURL, "api/assets/\(assetID)/trash")
    }

    func deleteURL(assetID: String) -> URL? {
        ServerEndpoint.path(baseURL, "api/assets/\(assetID)")
    }

    // MARK: - 通用请求

    private func get<T: Decodable>(path: String, query: [URLQueryItem] = []) async throws -> T {
        guard let url = ServerEndpoint.path(baseURL, path, query: query) else {
            throw ServerError.invalidAddress(baseURL.absoluteString)
        }
        await AppLog.shared.add(.info, "GET \(url.absoluteString)")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            throw mapURLError(error, url: url)
        } catch {
            throw ServerError.transport(message: error.localizedDescription, hint: nil)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ServerError.decoding("响应不是 HTTP")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw ServerError.http(status: http.statusCode, detail: Self.detail(from: data))
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw ServerError.decoding(String(describing: error))
        }
    }

    /// 服务端的错误体是 FastAPI 的 {"detail": "..."}
    static func detail(from data: Data) -> String {
        guard !data.isEmpty else { return "" }
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let detail = obj["detail"] as? String { return detail }
            if let detail = obj["detail"] as? [[String: Any]],
               let msg = detail.first?["msg"] as? String { return msg }
        }
        let text = String(data: data.prefix(300), encoding: .utf8) ?? ""
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func mapURLError(_ error: URLError, url: URL) -> ServerError {
        let hint: String?
        switch error.code {
        case .timedOut:
            hint = "服务器没响应。确认电脑没休眠、Docker 还在跑。"
        case .cannotFindHost, .dnsLookupFailed:
            hint = ServerEndpoint.isLocalNetwork(url)
                ? "域名解析不到。试试直接填 IP（如 192.168.31.115:7878），或让电脑和手机在同一个 Wi-Fi。"
                : "域名解析失败，检查地址拼写。"
        case .cannotConnectToHost, .networkConnectionLost:
            hint = ServerEndpoint.isLocalNetwork(url)
                ? "连不上主机。若这是第一次连接，请注意系统弹出的「本地网络」权限，允许后重试（设置 → 隐私与安全性 → 本地网络）。"
                : "连不上主机。"
        case .notConnectedToInternet, .internationalRoamingOff, .dataNotAllowed:
            hint = "手机网络不可用；确认 Wi-Fi 已连接。"
        case .appTransportSecurityRequiresSecureConnection:
            hint = "ATS 拦截了明文 HTTP；局域网地址需要 NSAllowsLocalNetworking（Info.plist 已配）。"
        default:
            hint = nil
        }
        return .transport(message: "网络错误（\(error.code.rawValue)）：\(error.localizedDescription)",
                          hint: hint)
    }
}

enum SettingsKeys {
    static let serverInput = "serverInput"
    static let effectiveBase = "effectiveBase"
    static let library = "library"
    static let options = "syncOptions"
    static let lastSyncAt = "lastSyncAt"
    static let authHeader = "authHeader"
}
