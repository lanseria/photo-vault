//
//  Models.swift
//  PhotoVaultSync
//
//  与 photo-vault 服务端 HTTP 接口一一对应的数据模型，以及全 App 共享的状态结构。
//  设计原则：
//    1. 解码全部容错 —— 服务器版本可能比 App 新/旧，缺字段不能让 App 崩；
//    2. 这里只放「纯数据」，不 import Photos / SwiftUI，方便单测。
//

import Foundation

// MARK: - 服务端：统计

/// GET /api/stats
struct RemoteStats: Decodable, Equatable, Sendable {
    var count: Int = 0
    var gallery: Int = 0
    var material: Int = 0
    var bytes: Int64 = 0
    var favorites: Int = 0
    var trash: Int = 0
    var persons: Int = 0
    var deviceCount: Int = 0
    var unknownDevice: Int = 0
    /// 库里最早 / 最晚一张的拍摄时间（带时区的 ISO8601 串）
    var first: String?
    var last: String?

    enum CodingKeys: String, CodingKey {
        case count, gallery, material, bytes, favorites, trash, persons, first, last
        case deviceCount = "device_count"
        case unknownDevice = "unknown_device"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        count = Self.int(c, .count)
        gallery = Self.int(c, .gallery)
        material = Self.int(c, .material)
        bytes = Self.int64(c, .bytes)
        favorites = Self.int(c, .favorites)
        trash = Self.int(c, .trash)
        persons = Self.int(c, .persons)
        deviceCount = Self.int(c, .deviceCount)
        unknownDevice = Self.int(c, .unknownDevice)
        first = try? c.decodeIfPresent(String.self, forKey: .first)
        last = try? c.decodeIfPresent(String.self, forKey: .last)
    }

    init() {}

    private static func int(_ c: KeyedDecodingContainer<CodingKeys>, _ k: CodingKeys) -> Int {
        (try? c.decodeIfPresent(Int.self, forKey: k)) ?? 0
    }

    private static func int64(_ c: KeyedDecodingContainer<CodingKeys>, _ k: CodingKeys) -> Int64 {
        if let v = try? c.decodeIfPresent(Int64.self, forKey: k) { return v }
        if let v = try? c.decodeIfPresent(Double.self, forKey: k) { return Int64(v) }
        return 0
    }
}

// MARK: - 服务端：资产

/// GET /api/assets 里的一项。字段名跟服务端 JSON 保持一致，只做 camelCase 别名。
struct RemoteAsset: Decodable, Identifiable, Equatable, Sendable {
    var id: String = ""
    /// photo | video | live_photo | raw | other
    var kind: String = "photo"
    var library: String = "gallery"
    var sha: String = ""
    var favorite: Bool = false
    /// 本地墙上时间（不带时区），如 2026-10-30T21:48:31
    var taken: String?
    var tzSource: String?
    /// 设备展示名（用户标签 > 归一化型号 > EXIF 原始型号）
    var model: String = ""
    var width: Int?
    var height: Int?
    var durationMs: Int?
    var bytes: Int64 = 0
    var hasThumb: Bool = false
    /// 原始文件名，如 IMG_0001.HEIC（同步时用来跟本机文件对账）
    var origin: String?
    var rawExt: String = ""
    var rawBytes: Int64 = 0
    var tags: [String] = []

    enum CodingKeys: String, CodingKey {
        case id, kind, library, sha, favorite, taken, model, width, height, bytes, origin, tags
        case tzSource = "tz_source"
        case durationMs = "duration_ms"
        case hasThumb = "has_thumb"
        case rawExt = "raw_ext"
        case rawBytes = "raw_bytes"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? "photo"
        library = (try? c.decodeIfPresent(String.self, forKey: .library)) ?? "gallery"
        sha = (try? c.decodeIfPresent(String.self, forKey: .sha)) ?? ""
        favorite = (try? c.decodeIfPresent(Bool.self, forKey: .favorite)) ?? false
        taken = try? c.decodeIfPresent(String.self, forKey: .taken)
        tzSource = try? c.decodeIfPresent(String.self, forKey: .tzSource)
        model = (try? c.decodeIfPresent(String.self, forKey: .model)) ?? ""
        width = try? c.decodeIfPresent(Int.self, forKey: .width)
        height = try? c.decodeIfPresent(Int.self, forKey: .height)
        durationMs = try? c.decodeIfPresent(Int.self, forKey: .durationMs)
        if let v = try? c.decodeIfPresent(Int64.self, forKey: .bytes) {
            bytes = v
        } else if let v = try? c.decodeIfPresent(Double.self, forKey: .bytes) {
            bytes = Int64(v)
        }
        hasThumb = (try? c.decodeIfPresent(Bool.self, forKey: .hasThumb)) ?? false
        origin = try? c.decodeIfPresent(String.self, forKey: .origin)
        rawExt = (try? c.decodeIfPresent(String.self, forKey: .rawExt)) ?? ""
        if let v = try? c.decodeIfPresent(Int64.self, forKey: .rawBytes) {
            rawBytes = v
        }
        tags = (try? c.decodeIfPresent([String].self, forKey: .tags)) ?? []
    }

    init() {}

    var isVideo: Bool { kind == "video" }
    var isLivePhoto: Bool { kind == "live_photo" }
    var isRaw: Bool { kind == "raw" }

    /// 原始文件名；没有就用 id 前 8 位兜底
    var fileName: String {
        if let origin, !origin.isEmpty {
            return (origin as NSString).lastPathComponent
        }
        return String(id.prefix(8))
    }

    var takenDate: Date? { ServerDate.parse(taken) }

    var pixelSizeText: String {
        guard let width, let height else { return "" }
        return "\(width)×\(height)"
    }

    var durationText: String? {
        guard let durationMs, durationMs > 0 else { return nil }
        let total = durationMs / 1000
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    /// 服务端列表里已经有这张的缩略图
    var thumbnailAvailable: Bool { hasThumb && sha.count == 64 }
}

/// GET /api/assets
struct AssetPage: Decodable, Sendable {
    var total: Int = 0
    var items: [RemoteAsset] = []

    enum CodingKeys: String, CodingKey { case total, items }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = (try? c.decodeIfPresent(Int.self, forKey: .total)) ?? 0
        items = (try? c.decodeIfPresent([RemoteAsset].self, forKey: .items)) ?? []
    }

    init() {}
}

/// POST /api/upload
///
/// status:
///   * `imported`  —— 新入库（Live Photo 的第二条也会是 imported，asset.kind 变 live_photo）
///   * `duplicate` —— 内容已在库里（按 sha256 去重），不产生副本
struct UploadResponse: Decodable, Sendable {
    var status: String = ""
    var asset: RemoteAsset?
    var queued: Int = 0
    var bytes: Int64 = 0

    enum CodingKeys: String, CodingKey { case status, asset, queued, bytes }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = (try? c.decodeIfPresent(String.self, forKey: .status)) ?? ""
        asset = try? c.decodeIfPresent(RemoteAsset.self, forKey: .asset)
        queued = (try? c.decodeIfPresent(Int.self, forKey: .queued)) ?? 0
        if let v = try? c.decodeIfPresent(Int64.self, forKey: .bytes) {
            bytes = v
        } else if let v = try? c.decodeIfPresent(Double.self, forKey: .bytes) {
            bytes = Int64(v)
        }
    }

    init() {}

    var isDuplicate: Bool { status == "duplicate" }
    var isSuccess: Bool { status == "imported" || status == "duplicate" }
}

// MARK: - 时间解析

enum ServerDate {
    /// 服务端 taken 是「本地墙上时间」（无时区），例如 2026-10-30T21:48:31。
    /// 我们只需要它来按天分组/展示，不需要换算绝对时刻。
    static let wall: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    static let wallFractional: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone.current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        return f
    }()

    static let display: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh-Hans_CN")
        f.dateFormat = "yyyy年M月d日 HH:mm"
        return f
    }()

    static let dayKey: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    static func parse(_ text: String?) -> Date? {
        guard let text, !text.isEmpty else { return nil }
        if let d = wall.date(from: text) { return d }
        if let d = wallFractional.date(from: text) { return d }
        if let d = ISO8601DateFormatter().date(from: text) { return d }
        return nil
    }

    static func displayText(_ text: String?) -> String {
        if let d = parse(text) { return display.string(from: d) }
        return text ?? "未知时间"
    }
}

// MARK: - 错误

enum ServerError: LocalizedError, Equatable {
    /// 地址填得不对（空、协议不支持、没有主机名）
    case invalidAddress(String)
    /// 还没配置服务器
    case notConfigured
    /// HTTP 非 2xx，detail 是服务端 {"detail": ...} 里的说明
    case http(status: Int, detail: String)
    /// 网络层：连不上、超时、本地网络权限没给……
    case transport(message: String, hint: String?)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .invalidAddress(let text):
            return "服务器地址无效：\(text)"
        case .notConfigured:
            return "还没有设置服务器地址"
        case .http(let status, let detail):
            return detail.isEmpty ? "服务器返回 HTTP \(status)" : detail
        case .transport(let message, let hint):
            return [message, hint].compactMap { $0 }.joined(separator: "\n")
        case .decoding(let message):
            return "解析服务器响应失败：\(message)"
        }
    }

    var isNetworkProblem: Bool {
        if case .transport = self { return true }
        return false
    }
}

// MARK: - 本机（PhotoKit）授权

enum PhotoAuthorizationStatus: String, Sendable {
    case unknown, notDetermined, denied, restricted, authorized, limited

    var isUsable: Bool { self == .authorized || self == .limited }

    var title: String {
        switch self {
        case .unknown: return "检查中…"
        case .notDetermined: return "未授权"
        case .denied: return "已拒绝"
        case .restricted: return "受限制"
        case .authorized: return "已授权（完整）"
        case .limited: return "已授权（部分照片）"
        }
    }
}

// MARK: - 同步选项（持久化到 UserDefaults）

struct SyncOptions: Codable, Equatable, Sendable {
    /// 同步照片
    var includePhotos = true
    /// 同步视频
    var includeVideos = true
    /// 同步 Live Photo（照片 + 动态视频两条资源）
    var includeLivePhotos = true
    /// 包含相册里隐藏的照片
    var includeHidden = false
    /// iCloud 上还没下载的原件，允许 App 拉下来再上传
    var allowICloudDownload = true
    /// 首次同步前先拉服务器索引：已经在服务器上的不再重复上传
    var skipExistingOnServer = true
    /// 并发上传数（局域网 2~3 比较稳）
    var concurrentUploads = 2
    /// 只同步这个时间之后拍的东西；nil = 全部
    var fromDate: Date?

    static let `default` = SyncOptions()

    var mediaSummary: String {
        var parts: [String] = []
        if includePhotos { parts.append("照片") }
        if includeVideos { parts.append("视频") }
        if includeLivePhotos { parts.append("实况") }
        return parts.isEmpty ? "未选择" : parts.joined(separator: " · ")
    }
}

// MARK: - 同步状态（引擎 → UI）

enum SyncPhase: String, Sendable {
    case idle
    case scanning
    case syncing
    case paused
    case finished
    case failed
}

struct SyncState: Equatable, Sendable {
    var phase: SyncPhase = .idle
    /// 本轮需要上传的资产总数（扫描完成后才准确）
    var total = 0
    /// 已处理完的（成功 + 跳过 + 失败）
    var completed = 0
    var succeeded = 0
    var skipped = 0
    var failed = 0
    /// 真正上传的字节数
    var bytesSent: Int64 = 0
    /// 当前正在上传的文件名
    var currentName = ""
    /// 一行给人看的说明
    var message = ""
    var startedAt: Date?
    var finishedAt: Date?
    var errorText: String?

    var isActive: Bool { phase == .scanning || phase == .syncing || phase == .paused }

    /// 0…1；total 未定时返回 nil（UI 用不确定进度条）
    var fraction: Double? {
        guard total > 0 else { return nil }
        return min(1, Double(completed) / Double(total))
    }

    var elapsed: TimeInterval? {
        guard let startedAt else { return nil }
        return (finishedAt ?? Date()).timeIntervalSince(startedAt)
    }

    /// 平均速度（字节/秒），用于显示 ETA
    var bytesPerSecond: Double? {
        guard let elapsed, elapsed > 1, bytesSent > 0 else { return nil }
        return Double(bytesSent) / elapsed
    }

    var eta: TimeInterval? {
        guard let fraction, fraction > 0.01, let elapsed else { return nil }
        return elapsed / fraction - elapsed
    }
}

/// 单个资产在同步队列里的状态
enum SyncItemState: String, Sendable {
    case pending, uploading, done, skipped, failed
}

struct SyncItem: Identifiable, Equatable, Sendable {
    var id: String
    /// 本机 PHAsset.localIdentifier（用来显示本地缩略图）
    var assetIdentifier: String?
    var name: String
    var state: SyncItemState
    var bytes: Int64 = 0
    var error: String?
    var updatedAt: Date = Date()
}

/// 本机相册 / 同步账本的汇总
struct LocalLibraryStats: Equatable, Sendable {
    var total = 0
    var synced = 0
    var failed = 0
    var pending = 0
    var bytesUploaded: Int64 = 0
    var lastSyncAt: Date?

    static let empty = LocalLibraryStats()

    var fraction: Double {
        guard total > 0 else { return 0 }
        return min(1, Double(synced) / Double(total))
    }
}

// MARK: - 服务器健康

enum ServerHealth: Equatable, Sendable {
    case unknown
    case checking
    case online(RemoteStats, URL)
    case offline(String)

    var isOnline: Bool { if case .online = self { return true }; return false }

    var summary: String {
        switch self {
        case .unknown: return "未检测"
        case .checking: return "连接中…"
        case .online(let stats, _):
            return "在线 · \(stats.count) 张 / \(Format.bytes(stats.bytes))"
        case .offline(let text): return "离线 · \(text)"
        }
    }
}

// MARK: - 格式化

enum Format {
    static func bytes(_ value: Int64) -> String {
        let units = ["B", "KB", "MB", "GB", "TB"]
        var size = Double(max(0, value))
        var i = 0
        while size >= 1024 && i < units.count - 1 {
            size /= 1024
            i += 1
        }
        if i == 0 { return "\(Int(size)) B" }
        return String(format: size >= 100 ? "%.0f %@" : "%.1f %@", size, units[i])
    }

    static func rate(_ bytesPerSecond: Double) -> String {
        bytes(Int64(bytesPerSecond)) + "/s"
    }

    static func duration(_ seconds: TimeInterval?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded())
        if total < 60 { return "\(total) 秒" }
        if total < 3600 { return "\(total / 60) 分 \(total % 60) 秒" }
        return "\(total / 3600) 时 \((total % 3600) / 60) 分"
    }
}
