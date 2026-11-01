//
//  LibraryView.swift
//  PhotoVaultSync
//
//  服务器图库（只读浏览）：紧凑页头 + 吸顶筛选条 + 按拍摄日分组的正方形网格。
//
//  设计要点（18k+ 张真实库的硬要求）：
//    * 正方形网格：cell 固定 1:1 + clipped，竖图/横图不再把行撑高低位错落；
//      列数自适应（iPhone 竖屏 3 列、横屏/iPad 自动变多），5pt 圆角 + 2pt 间隙；
//    * 加载体验：缩略图就绪后 0.2s 交叉淡入，占位是主题底色而不是死灰块；
//    * 角标：RAW/LIVE 左上、收藏右上（白心红底圆片）、视频右下 play+时长
//      （底部渐变压暗，浅色画面上时长依然可读；数据来自既有字段，不额外请求）；
//    * 按天分组：分组逻辑在 RemoteLibraryModel.groupByDay（纯函数、有单测），
//      节头毛玻璃 36pt、粘性吸顶；节与节之间留 6/10pt 呼吸；
//    * 滚动：距底 1.5 屏预取下一页（90 条/页），滚过 2 屏出现「回顶部」；
//    * 缩略图统一走 ImageIO 降采样（RemoteImageMetrics.thumbnailMaxPixelSize）。
//    * 日期：资产日期由服务端入库时确定；本 App 上传时把 PHAsset.creationDate
//      放进 taken query（UploadClient.takenQueryValue），文件没有内嵌时间的
//      资产（聊天保存图、截图重存）不再退化成"上传那一刻"。
//

import SwiftUI

struct LibraryView: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    /// 用户在图库里临时切的库（nil = 跟随设置页的目标库）
    @State private var libraryOverride: String?
    @State private var kindFilter: LibraryKindFilter = .all
    @State private var searchText = ""
    @State private var showSearch = false
    @State private var selected: RemoteAsset?
    @State private var showBackToTop = false
    @State private var skeletonPulse = false

    /// 自适应列数：iPhone 竖屏 3 列、横屏/iPad 自动变多（对齐 Apple Photos 的手感）。
    /// 间隙统一走 LazyVGrid(spacing: 2)，跟单元格 5pt 圆角配套，贴起来仍是整齐的一片。
    private var columns: [GridItem] {
        [GridItem(.adaptive(minimum: 106))]
    }

    private var library: String { libraryOverride ?? settings.library }
    private var libraryModel: RemoteLibraryModel { model.library }
    private var query: String? { RemoteLibraryModel.normalizedQuery(searchText) }

    /// 搜索词参与 key：输入时做 300ms 防抖，筛选/切库则立即生效
    private var reloadKey: String {
        "\(library)|\(kindFilter.rawValue)|\(query ?? "")"
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            if showSearch {
                searchBar
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            filterBar
            Rectangle()
                .fill(Color.white.opacity(0.08))
                .frame(height: 0.5)
            contentScroll
        }
        .background(VaultBackground())
        .task {
            await model.refreshStatsIfStale()
        }
        .task(id: reloadKey) {
            if !searchText.isEmpty {
                // 搜索输入防抖：连续输入只发最后一次请求
                try? await Task.sleep(nanoseconds: 300_000_000)
                if Task.isCancelled { return }
            }
            await libraryModel.loadIfNeeded(
                library: library, kind: kindFilter.serverKind, query: query
            )
        }
        .sheet(item: $selected) { asset in
            AssetDetailView(
                asset: asset,
                thumbURL: libraryModel.thumbURL(for: asset),
                previewURL: libraryModel.previewURL(for: asset)
            )
        }
    }

    // MARK: - 页头（紧凑：一行标题 + 一行小字，不再占 1/4 屏）

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("图库")
                .font(.system(.largeTitle, design: .rounded, weight: .bold))

            Spacer(minLength: 6)

            Text(headerSubtitle)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)

            Button {
                withAnimation(.easeOut(duration: 0.18)) {
                    showSearch.toggle()
                    if !showSearch { searchText = "" }
                }
            } label: {
                Image(systemName: showSearch ? "xmark" : "magnifyingglass")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(showSearch ? Theme.accent : Color.white.opacity(0.65))
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(Color.white.opacity(0.07)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(showSearch ? "关闭搜索" : "搜索图库")
        }
        .padding(.horizontal, Theme.hPadding)
        .padding(.top, 8)
        .padding(.bottom, 6)
    }

    private var headerSubtitle: String {
        if !model.hasServer { return "未连接服务器" }
        let count = libraryModel.total > 0 ? "\(libraryModel.total.formatted()) 张" : "…"
        return "只读 · \(settings.libraryTitle) · \(count)"
    }

    // MARK: - 搜索

    private var searchBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.secondary)

            TextField("搜索文件名 / 设备 / 标签", text: $searchText)
                .font(.system(.footnote, design: .rounded))
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .foregroundStyle(.primary)

            if !searchText.isEmpty {
                Button {
                    Haptics.tap()
                    searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 14))
                        .foregroundStyle(Color.white.opacity(0.4))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("清空搜索")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.black.opacity(0.28))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Color.white.opacity(0.09), lineWidth: 1)
        )
        .padding(.horizontal, Theme.hPadding)
        .padding(.bottom, 6)
    }

    // MARK: - 吸顶筛选条

    private var filterBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 7) {
                FilterChip(text: "相册", selected: library == "gallery") {
                    libraryOverride = "gallery"
                }
                FilterChip(text: "素材库", selected: library == "material") {
                    libraryOverride = "material"
                }

                Rectangle()
                    .fill(Color.white.opacity(0.14))
                    .frame(width: 1, height: 16)
                    .padding(.horizontal, 3)

                ForEach(LibraryKindFilter.allCases) { filter in
                    FilterChip(text: filter.title, selected: kindFilter == filter) {
                        kindFilter = filter
                    }
                }
            }
            .padding(.horizontal, Theme.hPadding)
            .padding(.vertical, 6)
        }
        .background(.ultraThinMaterial)
    }

    // MARK: - 滚动内容

    private var contentScroll: some View {
        ScrollViewReader { proxy in
            ScrollView {
                scrollContent
                    .background(offsetReader)
            }
            .coordinateSpace(name: ScrollSpace.name)
            .scrollIndicators(.hidden)
            .refreshable { await refresh() }
            .onPreferenceChange(ScrollOffsetPreferenceKey.self) { offset in
                // 滚过 2 屏才出现「回顶部」；只在状态翻转时更新，避免滚动中每帧刷新 body
                let shouldShow = offset > 1600
                if shouldShow != showBackToTop {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showBackToTop = shouldShow
                    }
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if showBackToTop {
                    backToTopButton(proxy: proxy)
                }
            }
        }
    }

    @ViewBuilder
    private var scrollContent: some View {
        LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
            // 回顶部的锚点（0 高，不影响布局）
            Color.clear
                .frame(height: 0)
                .id(ScrollSpace.topAnchorID)

            if !model.hasServer && libraryModel.isEmpty {
                serverEmptyCard
                    .padding(.horizontal, Theme.hPadding)
                    .padding(.top, 24)
            } else if libraryModel.isLoading && libraryModel.isEmpty {
                skeletonGrid
            } else if let error = libraryModel.errorText, libraryModel.isEmpty {
                errorCard(error)
                    .padding(.horizontal, Theme.hPadding)
                    .padding(.top, 24)
            } else if libraryModel.isEmpty {
                libraryEmptyCard
                    .padding(.horizontal, Theme.hPadding)
                    .padding(.top, 24)
            } else {
                ForEach(libraryModel.daySections) { section in
                    Section {
                        grid(for: section)
                    } header: {
                        DaySectionHeader(title: section.title, count: section.count)
                    }
                }
                footer
            }
        }
    }

    // MARK: - 网格

    private func grid(for section: LibraryDaySection) -> some View {
        LazyVGrid(columns: columns, spacing: 2) {
            ForEach(section.assets) { asset in
                AssetThumbnailCell(
                    asset: asset,
                    thumbURL: libraryModel.thumbURL(for: asset),
                    previewURL: libraryModel.previewURL(for: asset),
                    onOpen: { selected = asset }
                )
                .task {
                    await libraryModel.loadMoreIfNeeded(
                        current: asset,
                        library: library,
                        kind: kindFilter.serverKind,
                        query: query
                    )
                }
            }
        }
        .padding(.horizontal, 2)
        // 节与节之间留一点呼吸：上一节的收尾不顶着下一天的节头
        .padding(.top, 6)
        .padding(.bottom, 10)
    }

    @ViewBuilder
    private var footer: some View {
        if libraryModel.isLoadingMore {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small).tint(Theme.accent)
                Text("加载更多…").font(.caption2).foregroundStyle(.secondary)
            }
            .padding(.vertical, 14)
        } else if let error = libraryModel.loadMoreErrorText {
            VStack(spacing: 8) {
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(Theme.danger.opacity(0.9))
                    .multilineTextAlignment(.center)
                Button {
                    Task {
                        await libraryModel.retryLoadMore(
                            library: library, kind: kindFilter.serverKind, query: query
                        )
                    }
                } label: {
                    Label("重试", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VaultMiniButtonStyle())
                .accessibilityLabel("重试加载更多")
            }
            .padding(.vertical, 14)
        } else if !libraryModel.hasMore && !libraryModel.isEmpty {
            Text("已经到底了 · 共 \(libraryModel.total.formatted()) 张")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.vertical, 16)
        }
    }

    // MARK: - 占位 / 空 / 错误

    private var skeletonGrid: some View {
        LazyVGrid(columns: columns, spacing: 2) {
            ForEach(0..<18, id: \.self) { _ in
                Rectangle()
                    .fill(Color.white.opacity(0.05))
                    .aspectRatio(1, contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                    .overlay(
                        Image(systemName: "photo")
                            .font(.system(size: 15, weight: .light))
                            .foregroundStyle(Color.white.opacity(0.14))
                    )
            }
        }
        .padding(.horizontal, 2)
        .padding(.top, 2)
        .opacity(skeletonPulse ? 0.55 : 1)
        .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: skeletonPulse)
        .onAppear { skeletonPulse = true }
    }

    private var serverEmptyCard: some View {
        VaultCard {
            EmptyHint(
                icon: "server.rack",
                title: "还没有连接服务器",
                message: "连接家里的 Photo Vault 之后，这里会显示服务器上的照片。"
            )
            Button {
                model.goToSettings()
            } label: {
                Label("去设置", systemImage: "arrow.right.circle.fill")
            }
            .buttonStyle(VaultActionButtonStyle(variant: .primary))
            .accessibilityLabel("去设置连接服务器")
        }
    }

    private var libraryEmptyCard: some View {
        VaultCard {
            EmptyHint(
                icon: "tray",
                title: searchText.isEmpty ? "这个库里还没有内容" : "没有匹配的照片",
                message: searchText.isEmpty
                    ? "换一个目标库或筛选条件试试。"
                    : "换个关键词，或清空搜索再试。"
            )
        }
    }

    private func errorCard(_ error: String) -> some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.danger)
                        .frame(width: 32, height: 32)
                        .background(Circle().fill(Theme.danger.opacity(0.13)))
                    Text("图库加载失败")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                }
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Task { await refresh() }
                } label: {
                    Label("重试", systemImage: "arrow.clockwise")
                }
                .buttonStyle(VaultActionButtonStyle(variant: .secondary))
                .accessibilityLabel("重试加载图库")
            }
        }
    }

    // MARK: - 回顶部

    private func backToTopButton(proxy: ScrollViewProxy) -> some View {
        Button {
            Haptics.tap()
            withAnimation(.easeOut(duration: 0.35)) {
                proxy.scrollTo(ScrollSpace.topAnchorID, anchor: .top)
            }
        } label: {
            Image(systemName: "arrow.up")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 42, height: 42)
                .background(Circle().fill(.ultraThinMaterial))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.14), lineWidth: 1))
                .shadow(color: .black.opacity(0.35), radius: 10, y: 4)
        }
        .buttonStyle(.plain)
        .padding(.trailing, 16)
        .padding(.bottom, 96) // 避开 iOS 26 浮动 Tab 栏
        .transition(.opacity.combined(with: .scale(scale: 0.9)))
        .accessibilityLabel("回到顶部")
    }

    // MARK: - 数据

    private var offsetReader: some View {
        GeometryReader { geo in
            Color.clear.preference(
                key: ScrollOffsetPreferenceKey.self,
                value: -geo.frame(in: .named(ScrollSpace.name)).minY
            )
        }
    }

    private func refresh() async {
        RemoteImageStore.shared.resetFailures()
        await model.refreshStatsIfStale(maxAge: 0)
        await libraryModel.reload(library: library, kind: kindFilter.serverKind, query: query)
    }
}

// MARK: - 网格单元格

/// 正方形缩略图单元：加载占位 / 失败重试 / 无缩略图 + 角标（RAW·LIVE / 视频时长 / 收藏）。
struct AssetThumbnailCell: View {
    let asset: RemoteAsset
    let thumbURL: URL?
    let previewURL: URL?
    let onOpen: () -> Void

    private enum LoadState {
        case loading
        case loaded(UIImage)
        case failed
        case unavailable
    }

    @State private var state: LoadState = .loading
    @State private var retryToken = 0

    var body: some View {
        Color.clear
            .aspectRatio(1, contentMode: .fit)
            .overlay { imageLayer }
            .overlay(alignment: .topLeading) { leftBadge }
            .overlay(alignment: .topTrailing) { favoriteBadge }
            .overlay(alignment: .bottomTrailing) { videoBadge }
            .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
            .contentShape(Rectangle())
            .onTapGesture {
                switch state {
                case .failed:
                    retry()
                case .loading, .loaded, .unavailable:
                    Haptics.tap()
                    onOpen()
                }
            }
            .task(id: "\(thumbURL?.absoluteString ?? "-")|\(retryToken)") {
                await load()
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(accessibilityLabel)
            .accessibilityAddTraits(.isButton)
    }

    // MARK: 图片层

    /// 图片是否已经就绪（加载 → 就绪时做一次 0.2s 交叉淡入，替代生硬的「啪一下出现」）
    private var isLoaded: Bool {
        if case .loaded = state { return true }
        return false
    }

    @ViewBuilder
    private var imageLayer: some View {
        ZStack {
            // 主题底色占位（不是死灰块）
            Rectangle()
                .fill(
                    LinearGradient(
                        colors: [Color.white.opacity(0.055), Color.white.opacity(0.035)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )

            switch state {
            case .loading:
                Image(systemName: "photo")
                    .font(.system(size: 15, weight: .light))
                    .foregroundStyle(Color.white.opacity(0.20))

            case .loaded(let image):
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipped()
                    .transition(.opacity)

            case .failed:
                VStack(spacing: 3) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 15, weight: .medium))
                    Text("重试")
                        .font(.system(size: 9, weight: .medium, design: .rounded))
                }
                .foregroundStyle(Color.white.opacity(0.55))

            case .unavailable:
                VStack(spacing: 3) {
                    Image(systemName: "photo")
                        .font(.system(size: 14, weight: .light))
                    Text("无缩略图")
                        .font(.system(size: 8, weight: .medium, design: .rounded))
                }
                .foregroundStyle(Color.white.opacity(0.30))
            }
        }
        .animation(.easeOut(duration: 0.2), value: isLoaded)
    }

    // MARK: 角标（统一胶囊：黑 55% 底、白字 11pt）

    @ViewBuilder
    private var leftBadge: some View {
        if let text = leftBadgeText {
            badgePill(text)
                .padding(3)
                .allowsHitTesting(false)
        }
    }

    /// RAW 优先于 LIVE（二者同一位置，最多显示一个）
    private var leftBadgeText: String? {
        if !asset.rawExt.isEmpty { return "RAW" }
        if asset.isLivePhoto { return "LIVE" }
        return nil
    }

    @ViewBuilder
    private var favoriteBadge: some View {
        if asset.favorite {
            // 白心 + 红底圆片：亮色照片上也看得清（比裸红心对比度高一档）
            Image(systemName: "heart.fill")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 17, height: 17)
                .background(Circle().fill(Theme.danger.opacity(0.9)))
                .shadow(color: .black.opacity(0.35), radius: 2, y: 1)
                .padding(4)
                .allowsHitTesting(false)
        }
    }

    @ViewBuilder
    private var videoBadge: some View {
        if asset.isVideo {
            // 底部渐变压暗 + play/时长：不遮挡主体，浅色画面上时长依然可读
            ZStack(alignment: .bottomTrailing) {
                LinearGradient(
                    colors: [.clear, .black.opacity(0.42)],
                    startPoint: .center,
                    endPoint: .bottom
                )
                HStack(spacing: 3) {
                    Image(systemName: "play.fill")
                        .font(.system(size: 8, weight: .bold))
                    if let duration = asset.durationText {
                        Text(duration)
                            .font(.system(size: 10, weight: .semibold, design: .rounded))
                            .monospacedDigit()
                    }
                }
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.4), radius: 2, y: 1)
                .padding(.trailing, 5)
                .padding(.bottom, 4)
            }
            .allowsHitTesting(false)
        }
    }

    private func badgePill(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .tracking(0.4)
            .foregroundStyle(.white)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(Capsule().fill(.black.opacity(0.55)))
    }

    // MARK: 行为

    private func load() async {
        guard let thumbURL else {
            state = .unavailable
            return
        }
        if case .loaded = state { return }
        state = .loading
        let image = await RemoteImageStore.shared.thumbnail(thumb: thumbURL, preview: previewURL)
        guard !Task.isCancelled else { return }
        if let image {
            state = .loaded(image)
        } else {
            state = .failed
        }
    }

    private func retry() {
        Haptics.tap()
        RemoteImageStore.shared.retry(thumb: thumbURL, preview: previewURL)
        retryToken += 1
    }

    private var accessibilityLabel: String {
        var parts = [AssetKind.label(asset.kind)]
        if let date = asset.takenDate {
            parts.append(TimeText.day(date))
        }
        if !asset.model.isEmpty {
            parts.append(asset.model)
        }
        if asset.isVideo, let duration = asset.durationText {
            parts.append("时长 \(duration)")
        }
        if asset.favorite {
            parts.append("已收藏")
        }
        return parts.joined(separator: "，")
    }
}

// MARK: - 日期节头

/// 粘性节头（36pt 毛玻璃）：左侧琥珀小竖条 + 「今天/昨天/M月d日（跨年带年份）」，右当天张数
struct DaySectionHeader: View {
    let title: String
    let count: Int

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            RoundedRectangle(cornerRadius: 1, style: .continuous)
                .fill(Theme.accent.opacity(0.85))
                .frame(width: 3, height: 13)
            Text(title)
                .font(.system(.subheadline, design: .rounded, weight: .semibold))
            Spacer(minLength: 8)
            Text("\(count) 张")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .frame(maxWidth: .infinity)
        .frame(height: 36)
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Color.white.opacity(0.07))
                .frame(height: 0.5)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title)，\(count) 张")
    }
}

// MARK: - 滚动辅助

private enum ScrollSpace {
    static let name = "library-scroll"
    static let topAnchorID = "library-top"
}

private struct ScrollOffsetPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

// MARK: - 筛选项

enum LibraryKindFilter: String, CaseIterable, Identifiable {
    case all
    case photos
    case videos

    var id: String { rawValue }

    var title: String {
        switch self {
        case .all: return "全部"
        case .photos: return "照片"
        case .videos: return "视频"
        }
    }

    /// 传给服务端 kind 参数的值（服务端支持逗号分隔）
    var serverKind: String? {
        switch self {
        case .all: return nil
        case .photos: return "photo,raw,live_photo"
        case .videos: return "video"
        }
    }
}
