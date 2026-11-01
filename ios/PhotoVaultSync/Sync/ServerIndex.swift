//
//  ServerIndex.swift
//  PhotoVaultSync
//
//  服务器索引对账：start() 时翻页拉 GET /api/assets 建一份「服务器上已有什么」的索引，
//  本地规划出来的上传组如果能匹配上，就记为 skipped，不再重传。
//
//  匹配规则（实测踩过坑，别简化）：
//    * 文件名：origin 可能是 "身材记录/IMG_4610.HEIC"、"2026/10/17/IMG_4606.HEIC"、
//      也可能带反斜杠，必须取 basename 再小写比较；
//    * 宽高：服务器存的可能是旋转后的（3024×4032 vs 4032×3024），排序成 (短边,长边)
//      再比，并留 2% 容差；
//    * 拍摄日期：服务器 taken 是「本地墙上时间」，比到分钟没意义，按 ±1 天；
//    * 视频再比时长 ±2s；
//    * 任一侧字段缺失就不拿它当否决条件（老数据/服务端版本差异）。
//
//  拉取失败不能卡死同步：拿到多少用多少，一条都没拿到就当没有索引（全量上传，
//  服务端按 sha256 去重兜底），并在 state.message 里告诉用户。
//

import Foundation

// MARK: - 索引条目

struct ServerIndexEntry: Equatable, Sendable {
    var id: String
    var kind: String
    /// 小写 basename，匹配用
    var nameKey: String
    var width: Int?
    var height: Int?
    var taken: Date?
    var durationMs: Int?

    init(remote: RemoteAsset) {
        id = remote.id
        kind = remote.kind.lowercased()
        // origin 才是原始文件名；RemoteAsset.fileName 也会取 basename，但拿不到时用 id 兜底
        nameKey = SyncPlanner.nameKey(remote.origin ?? remote.fileName)
        width = remote.width
        height = remote.height
        taken = remote.takenDate
        durationMs = remote.durationMs
    }

    init(id: String, kind: String, nameKey: String, width: Int? = nil,
         height: Int? = nil, taken: Date? = nil, durationMs: Int? = nil) {
        self.id = id
        self.kind = kind
        self.nameKey = nameKey
        self.width = width
        self.height = height
        self.taken = taken
        self.durationMs = durationMs
    }
}

struct ServerIndex: Equatable, Sendable {
    private(set) var entries: [ServerIndexEntry] = []
    /// nameKey → 条目下标（同名多张，比如连拍/不同目录同名）
    private var buckets: [String: [Int]] = [:]

    init(entries: [ServerIndexEntry] = []) {
        self.entries = entries
        var buckets: [String: [Int]] = [:]
        buckets.reserveCapacity(entries.count)
        for (index, entry) in entries.enumerated() where !entry.nameKey.isEmpty {
            buckets[entry.nameKey, default: []].append(index)
        }
        self.buckets = buckets
    }

    var count: Int { entries.count }
    var isEmpty: Bool { entries.isEmpty }

    /// 上传组是否已经在服务器上（返回命中的服务器条目）
    func match(group: UploadGroup) -> ServerIndexEntry? {
        guard !group.matchNameKey.isEmpty,
              let indexes = buckets[group.matchNameKey] else { return nil }
        for index in indexes where Self.matches(group: group, entry: entries[index]) {
            return entries[index]
        }
        return nil
    }

    /// 只按「静帧」信息找：不看 kind 白名单，只看名字 / 宽高 / 日期。
    /// Live Photo 专用：如果服务器上只有静帧（kind=photo）而没有实况动态，
    /// 也绝不能单独把动态传上去 —— 服务端会把它当成一条孤儿 video。
    func matchStill(of group: UploadGroup) -> ServerIndexEntry? {
        guard !group.matchNameKey.isEmpty,
              let indexes = buckets[group.matchNameKey] else { return nil }
        for index in indexes {
            let entry = entries[index]
            guard ["photo", "live_photo", "raw"].contains(entry.kind) else { continue }
            guard Self.dimensionsMatch(width: group.pixelWidth, height: group.pixelHeight,
                                       entry: entry) else { continue }
            guard Self.datesMatch(group.takenAt, entry.taken) else { continue }
            return entry
        }
        return nil
    }

    // MARK: 匹配规则

    static func matches(group: UploadGroup, entry: ServerIndexEntry) -> Bool {
        guard !group.matchNameKey.isEmpty, group.matchNameKey == entry.nameKey else { return false }
        guard group.allowedServerKinds.contains(entry.kind) else { return false }
        guard dimensionsMatch(width: group.pixelWidth, height: group.pixelHeight, entry: entry) else { return false }
        guard datesMatch(group.takenAt, entry.taken) else { return false }
        guard durationsMatch(localMs: group.durationMs, serverMs: entry.durationMs) else { return false }
        return true
    }

    /// 旋转后的宽高不算不一致；缺字段时不否决
    static func dimensionsMatch(width: Int, height: Int, entry: ServerIndexEntry) -> Bool {
        guard width > 0, height > 0,
              let serverWidth = entry.width, serverWidth > 0,
              let serverHeight = entry.height, serverHeight > 0 else { return true }
        let local = (min(width, height), max(width, height))
        let server = (min(serverWidth, serverHeight), max(serverWidth, serverHeight))
        return closeEnough(local.0, server.0) && closeEnough(local.1, server.1)
    }

    /// ±1 天（按绝对时间 1.5 天容差，吸收时区/午夜边界误差）
    static func datesMatch(_ local: Date?, _ server: Date?) -> Bool {
        guard let local, let server else { return true }
        return abs(local.timeIntervalSince(server)) <= 86_400 * 1.5
    }

    /// 视频时长 ±2s
    static func durationsMatch(localMs: Int?, serverMs: Int?) -> Bool {
        guard let localMs, localMs > 0, let serverMs, serverMs > 0 else { return true }
        return abs(localMs - serverMs) <= 2_000
    }

    private static func closeEnough(_ a: Int, _ b: Int) -> Bool {
        let allowed = max(2, Int(Double(max(a, b)) * 0.02))
        return abs(a - b) <= allowed
    }
}

// MARK: - 拉取

struct ServerIndexResult: Sendable {
    var index: ServerIndex
    /// 非 nil 表示索引不完整（或完全没拿到），UI/日志要提示用户
    var degradedNote: String?
    var isComplete: Bool

    static func empty(note: String) -> ServerIndexResult {
        ServerIndexResult(index: ServerIndex(), degradedNote: note, isComplete: false)
    }
}

@MainActor
enum ServerIndexLoader {
    static let pageSize = 300
    /// 2 万张 ≈ 67 页；留足余量但设上限，避免服务器异常返回永远翻不完
    static let maxPages = 500

    /// 翻页拉全量索引。永不 throw：失败降级为「空索引 / 部分索引 + 提示」。
    static func load(client: APIClient,
                     library: String,
                     onProgress: ((_ fetched: Int, _ total: Int) -> Void)? = nil,
                     shouldStop: (() -> Bool)? = nil) async -> ServerIndexResult {
        var entries: [ServerIndexEntry] = []
        var total = 0
        var offset = 0
        var pages = 0
        var failureNote: String?

        while pages < maxPages {
            if shouldStop?() == true { break }
            do {
                let page = try await fetchPageWithRetry(client: client, library: library, offset: offset)
                total = page.total
                entries.append(contentsOf: page.items.map(ServerIndexEntry.init(remote:)))
                pages += 1
                offset += page.items.count
                onProgress?(entries.count, total)
                if page.items.isEmpty || offset >= total { break }
                // 54 页连续请求会把服务器和主线程都顶满，每页之间让出 60ms
                try? await Task.sleep(nanoseconds: 60_000_000)
            } catch is CancellationError {
                break
            } catch {
                failureNote = (error as? ServerError)?.localizedDescription ?? error.localizedDescription
                break
            }
        }

        let complete = failureNote == nil && (total == 0 || offset >= total)
        var note: String?
        if let failureNote {
            if entries.isEmpty {
                note = "服务器索引拉取失败（\(failureNote)），本轮改为逐项上传；重复内容服务端会按内容去重"
            } else {
                note = "服务器索引只拿到 \(entries.count)/\(max(total, entries.count)) 条（\(failureNote)），其余按上传处理；重复内容服务端会去重"
            }
        }
        return ServerIndexResult(index: ServerIndex(entries: entries),
                                 degradedNote: note,
                                 isComplete: complete)
    }

    /// 单页失败重试一次（局域网抖动很常见），第二次还失败就交给上层降级
    private static func fetchPageWithRetry(client: APIClient,
                                           library: String,
                                           offset: Int) async throws -> AssetPage {
        do {
            return try await client.assets(limit: pageSize, offset: offset, library: library)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            try? await Task.sleep(nanoseconds: 400_000_000)
            return try await client.assets(limit: pageSize, offset: offset, library: library)
        }
    }
}
