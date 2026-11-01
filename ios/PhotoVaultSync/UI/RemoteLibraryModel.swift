//
//  RemoteLibraryModel.swift
//  PhotoVaultSync
//
//  服务器图库页的数据源：/api/assets 分页（90 张/页）、按拍摄日分组、服务端搜索、
//  下拉刷新、接近底部预取下一页。
//  只读：只发 GET 请求，没有任何写接口。
//

import Foundation
import Observation

// MARK: - 按天分组

/// 图库按「拍摄日」分组后的一节；节头显示 今天/昨天/M月d日（跨年带年份）/未知时间。
struct LibraryDaySection: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let assets: [RemoteAsset]

    var count: Int { assets.count }
}

@MainActor
@Observable
final class RemoteLibraryModel {
    private let settings: SettingsStore

    private(set) var assets: [RemoteAsset] = []
    private(set) var total = 0
    private(set) var isLoading = false
    private(set) var isLoadingMore = false
    private(set) var errorText: String?
    private(set) var loadMoreErrorText: String?
    private(set) var hasMore = true

    /// 当前已加载数据对应的筛选条件（切 Tab 回来时用来判断要不要重载）
    private(set) var loadedLibrary = "gallery"
    private(set) var loadedKind: String?
    private(set) var loadedQuery: String?

    /// 每次筛选/刷新递增；旧请求回来时直接丢弃
    private var generation = 0
    private var needsReload = true

    /// 服务端每页条数（保持 90）
    let pageSize = 90

    /// 距底部还剩多少条时开始预取下一页（36 条 ≈ 12 行 ≈ 1.5 屏）
    private let prefetchDistance = 36

    private var cachedClient: APIClient?
    private var cachedClientBase: URL?

    /// 搜索分页用的临时会话（APIClient.assets 还没有 q 参数，见 searchPage 注释）
    private static let searchSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 15
        cfg.timeoutIntervalForResource = 60
        cfg.waitsForConnectivity = false
        cfg.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: cfg)
    }()

    init(settings: SettingsStore) {
        self.settings = settings
    }

    // MARK: - 派生

    var isEmpty: Bool { assets.isEmpty }

    var statusText: String {
        if isLoading { return "加载中…" }
        if let errorText { return "加载失败：\(errorText)" }
        if isEmpty { return "这个库还没有内容" }
        return "已加载 \(assets.count) / 共 \(total) 张"
    }

    /// 当前数据按拍摄日分组后的结果（View 直接渲染；分组逻辑是纯函数，有单测）
    var daySections: [LibraryDaySection] {
        Self.groupByDay(assets)
    }

    // MARK: - 分组（纯函数，单测覆盖）

    /// 按拍摄日分组：
    /// - 服务器已按拍摄时间倒序返回，这里按天倒序、同一天内保持服务器顺序；
    /// - `taken == nil` 的归入「未知时间」，固定放在最后；
    /// - 今天/昨天用相对文案，同年只写「M月d日」，跨年带年份。
    nonisolated static func groupByDay(_ assets: [RemoteAsset],
                                       now: Date = Date(),
                                       calendar: Calendar = .current) -> [LibraryDaySection] {
        let today = calendar.startOfDay(for: now)
        let yesterday = calendar.date(byAdding: .day, value: -1, to: today) ?? today

        var buckets: [Date: [RemoteAsset]] = [:]
        var unknown: [RemoteAsset] = []
        for asset in assets {
            guard let date = asset.takenDate else {
                unknown.append(asset)
                continue
            }
            let day = calendar.startOfDay(for: date)
            buckets[day, default: []].append(asset)
        }

        var sections: [LibraryDaySection] = buckets.keys.sorted(by: >).map { day in
            LibraryDaySection(
                id: dayID(day, calendar: calendar),
                title: dayTitle(day, now: now, today: today, yesterday: yesterday, calendar: calendar),
                assets: buckets[day] ?? []
            )
        }
        if !unknown.isEmpty {
            sections.append(LibraryDaySection(id: "unknown", title: "未知时间", assets: unknown))
        }
        return sections
    }

    /// 稳定 id（yyyy-MM-dd，按传入 calendar 的时区）
    nonisolated static func dayID(_ day: Date, calendar: Calendar) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: day)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    nonisolated static func dayTitle(_ day: Date, now: Date,
                                     today: Date, yesterday: Date,
                                     calendar: Calendar) -> String {
        if calendar.isDate(day, inSameDayAs: today) { return "今天" }
        if calendar.isDate(day, inSameDayAs: yesterday) { return "昨天" }
        let c = calendar.dateComponents([.year, .month, .day], from: day)
        let year = c.year ?? 0
        let month = c.month ?? 0
        let dayOfMonth = c.day ?? 0
        if year == calendar.component(.year, from: now) {
            return "\(month)月\(dayOfMonth)日"
        }
        return "\(year)年\(month)月\(dayOfMonth)日"
    }

    /// 搜索词规范化（纯函数，单测覆盖）：空白 = 不过滤；否则去掉首尾空白。
    nonisolated static func normalizedQuery(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    // MARK: - URL

    private func client() -> APIClient? {
        let base = settings.resolvedBase
        if let cachedClient, cachedClientBase == base { return cachedClient }
        guard let created = try? settings.makeClient() else { return nil }
        cachedClient = created
        cachedClientBase = base
        return created
    }

    func thumbURL(for asset: RemoteAsset) -> URL? {
        guard asset.thumbnailAvailable, let client = client() else { return nil }
        return client.thumbURL(sha: asset.sha)
    }

    func previewURL(for asset: RemoteAsset) -> URL? {
        client()?.previewURL(assetID: asset.id)
    }

    // MARK: - 加载

    /// 首次进入页面 / 切 Tab 回来时调用：条件没变且已有数据就不重复请求
    func loadIfNeeded(library: String, kind: String?, query: String? = nil) async {
        if !assets.isEmpty, !needsReload,
           loadedLibrary == library, loadedKind == kind, loadedQuery == query {
            return
        }
        await reload(library: library, kind: kind, query: query)
    }

    /// 让下次 loadIfNeeded 重新拉（服务器地址/目标库变化后调用）
    func invalidate() {
        needsReload = true
    }

    /// 下拉刷新 / 切换筛选 / 改搜索词：从第一页重新拉
    func reload(library: String, kind: String?, query: String? = nil) async {
        generation += 1
        let gen = generation
        loadedLibrary = library
        loadedKind = kind
        loadedQuery = query
        needsReload = false
        if assets.isEmpty { isLoading = true }
        errorText = nil
        loadMoreErrorText = nil

        do {
            guard let client = client() else { throw ServerError.notConfigured }
            let page = try await fetchPage(client: client, offset: 0,
                                           library: library, kind: kind, query: query)
            guard gen == generation else { return }
            assets = page.items
            total = page.total
            hasMore = page.items.count >= pageSize && assets.count < max(page.total, assets.count)
            isLoading = false
            AppLog.shared.add(.info, "图库加载 \(page.items.count) 条 · 共 \(page.total) 张"
                              + (query.map { " · 搜索「\($0)」" } ?? ""))
        } catch {
            guard gen == generation else { return }
            isLoading = false
            errorText = Self.describe(error)
            if assets.isEmpty { total = 0; hasMore = false }
            AppLog.shared.add(.warn, "图库加载失败：\(errorText ?? "")")
        }
    }

    /// 接近底部时增量加载
    func loadMore(library: String, kind: String?, query: String? = nil) async {
        guard !isLoading, !isLoadingMore, hasMore,
              errorText == nil, loadMoreErrorText == nil else { return }
        // 筛选条件变了：先整页重载，不要在旧数据后面追加
        if loadedLibrary != library || loadedKind != kind || loadedQuery != query {
            await reload(library: library, kind: kind, query: query)
            return
        }
        isLoadingMore = true
        defer { isLoadingMore = false }
        let gen = generation

        do {
            guard let client = client() else { throw ServerError.notConfigured }
            let page = try await fetchPage(client: client, offset: assets.count,
                                           library: library, kind: kind, query: query)
            guard gen == generation else { return }
            let existing = Set(assets.map(\.id))
            assets.append(contentsOf: page.items.filter { !existing.contains($0.id) })
            total = max(page.total, assets.count)
            hasMore = page.items.count >= pageSize && assets.count < total
        } catch {
            guard gen == generation else { return }
            loadMoreErrorText = Self.describe(error)
        }
    }

    /// 滚动到「距底部 1.5 屏」内时自动预取下一页
    func loadMoreIfNeeded(current: RemoteAsset, library: String, kind: String?, query: String? = nil) async {
        guard let index = assets.firstIndex(where: { $0.id == current.id }) else { return }
        guard index >= assets.count - prefetchDistance else { return }
        await loadMore(library: library, kind: kind, query: query)
    }

    func retryLoadMore(library: String, kind: String?, query: String? = nil) async {
        loadMoreErrorText = nil
        await loadMore(library: library, kind: kind, query: query)
    }

    // MARK: - 请求

    private func fetchPage(client: APIClient, offset: Int,
                           library: String, kind: String?, query: String?) async throws -> AssetPage {
        if let query, !query.isEmpty {
            return try await searchPage(client: client, offset: offset,
                                        library: library, kind: kind, query: query)
        }
        return try await client.assets(limit: pageSize, offset: offset, library: library, kind: kind)
    }

    /// ⚠️ 临时：Core 的 `APIClient.assets` 还没有 `q` 参数（Core 归 lead），
    /// 这里用同一个 baseURL + 同一套错误映射补一个搜索分页。
    /// 等 Core 加上 `q` 后删掉这段，直接换成 client.assets(..., q: query)。
    private func searchPage(client: APIClient, offset: Int,
                            library: String, kind: String?, query: String) async throws -> AssetPage {
        var items: [URLQueryItem] = [
            URLQueryItem(name: "limit", value: String(pageSize)),
            URLQueryItem(name: "offset", value: String(offset)),
            URLQueryItem(name: "library", value: library),
            URLQueryItem(name: "q", value: query),
        ]
        if let kind, !kind.isEmpty {
            items.append(URLQueryItem(name: "kind", value: kind))
        }
        guard let url = ServerEndpoint.path(client.baseURL, "api/assets", query: items) else {
            throw ServerError.invalidAddress(client.baseURL.absoluteString)
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        AppLog.shared.add(.info, "GET \(url.absoluteString)")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.searchSession.data(for: request)
        } catch let error as URLError {
            throw client.mapURLError(error, url: url)
        } catch {
            throw ServerError.transport(message: error.localizedDescription, hint: nil)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ServerError.decoding("响应不是 HTTP")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw ServerError.http(status: http.statusCode, detail: APIClient.detail(from: data))
        }
        do {
            return try JSONDecoder().decode(AssetPage.self, from: data)
        } catch {
            throw ServerError.decoding(String(describing: error))
        }
    }

    // MARK: - 工具

    private static func describe(_ error: Error) -> String {
        if let error = error as? ServerError { return error.localizedDescription }
        return error.localizedDescription
    }
}
