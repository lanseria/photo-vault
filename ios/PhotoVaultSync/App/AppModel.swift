//
//  AppModel.swift
//  PhotoVaultSync
//
//  全 App 的状态根：持有设置、同步引擎、服务器健康状态和图库数据源。
//  UI 层通过 @Environment(AppModel.self) 取用。
//
//  设计要点：
//    * bootstrap() 幂等 —— RootView 的 .task / 回前台都会调用，重复调用不会重复初始化；
//    * 健康状态分两层：SettingsStore 负责「连得上吗」（真实请求），AppModel 只缓存结论；
//    * 同步进度不在这里镜像，UI 直接观察 engine.state（@Observable 自动刷新）。
//

import Foundation
import Observation
import SwiftUI

/// 底部三个 Tab
enum AppTab: Hashable {
    case sync
    case library
    case settings
}

@MainActor
@Observable
final class AppModel {
    let settings: SettingsStore
    let engine: SyncEngine
    /// 服务器图库页的分页数据源（只读）
    let library: RemoteLibraryModel

    /// 服务器连接状态（同步页顶部那张卡）
    private(set) var health: ServerHealth = .unknown
    /// 服务器统计（连接成功后填充）
    private(set) var remoteStats: RemoteStats?
    /// 冷启动流程是否跑完（用来决定是否显示首次引导）
    private(set) var isReady = false
    /// 下拉刷新进行中（避免连点）
    private(set) var isRefreshing = false
    /// 最近一次探测成功/失败的时间（回前台时做节流）
    private(set) var lastCheckedAt: Date?

    /// 当前 Tab（放这里是为了让同步页的「去设置」能直接切页）
    var selectedTab: AppTab = .sync

    private var isBootstrapping = false

    // MARK: - init

    init() {
        let settings = SettingsStore()
        self.settings = settings
        self.engine = SyncEngine(settings: settings)
        self.library = RemoteLibraryModel(settings: settings)
    }

    init(settings: SettingsStore) {
        self.settings = settings
        self.engine = SyncEngine(settings: settings)
        self.library = RemoteLibraryModel(settings: settings)
    }

    // MARK: - 生命周期

    /// 冷启动：引擎初始化 + 静默探测服务器。幂等，可以重复调用。
    func bootstrap() async {
        guard !isReady, !isBootstrapping else { return }
        isBootstrapping = true
        AppLog.shared.add(.info, "App 启动，开始 bootstrap")

        await engine.bootstrap()
        await probeSilently()

        isBootstrapping = false
        isReady = true
        AppLog.shared.add(.info, "bootstrap 完成：\(health.summary)")
    }

    func handleScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .active:
            Task {
                await engine.handleForeground()
                // 回前台节流：60 秒内刚刚探测过就不重复连
                if let last = lastCheckedAt, Date().timeIntervalSince(last) < 60 { return }
                // silentProbe 也能在没存 effectiveBase 时自动试输入地址/默认地址
                await probeSilently()
            }
        case .background:
            Task { await engine.handleBackground() }
        default:
            break
        }
    }

    // MARK: - 服务器

    /// 静默探测（不弹提示）：走 SettingsStore.silentProbe()，失败只更新 health。
    func probeSilently() async {
        health = .checking
        if let stats = await settings.silentProbe(),
           let base = settings.effectiveBase ?? settings.resolvedBase {
            remoteStats = stats
            health = .online(stats, base)
            lastCheckedAt = Date()
        } else {
            let text = settings.lastProbeError?.localizedDescription ?? "连接失败"
            health = .offline(text)
            lastCheckedAt = Date()
            AppLog.shared.add(.warn, "服务器静默探测失败：\(text)")
        }
    }

    /// 手动连接测试（设置页 / 同步页按钮调用）
    @discardableResult
    func testConnection() async -> RemoteStats? {
        health = .checking
        let result = await settings.testConnection()
        lastCheckedAt = Date()
        switch result {
        case .success(let stats):
            remoteStats = stats
            if let base = settings.effectiveBase ?? settings.resolvedBase {
                health = .online(stats, base)
            } else {
                health = .unknown
            }
            // 服务器/目标库可能变了，图库下次进入重新拉
            library.invalidate()
            return stats
        case .failure(let error):
            health = .offline(error.localizedDescription)
            return nil
        }
    }

    /// 页面出现时刷新服务器统计（节流）：服务器上数字会持续变（用户还在同步），
    /// 但不必每次切 Tab 都打一次 /api/stats。
    func refreshStatsIfStale(maxAge: TimeInterval = 60) async {
        guard settings.effectiveBase != nil else { return }
        if let last = lastCheckedAt, Date().timeIntervalSince(last) < maxAge { return }
        await probeSilently()
    }

    /// 同步页下拉刷新：重新探测服务器 + 刷新本机统计
    func refreshAll() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        await engine.refreshLocalStats()

        if settings.effectiveBase == nil {
            // 还没确认过地址：直接走完整连接测试（会同时尝试 .local 候选）
            let result = await settings.testConnection()
            lastCheckedAt = Date()
            switch result {
            case .success(let stats):
                remoteStats = stats
                if let base = settings.effectiveBase ?? settings.resolvedBase {
                    health = .online(stats, base)
                }
                library.invalidate()
            case .failure(let error):
                health = .offline(error.localizedDescription)
            }
        } else {
            await probeSilently()
        }
        AppLog.shared.add(.info, "下拉刷新完成：\(health.summary)")
    }

    // MARK: - 便捷

    var serverAddressText: String {
        settings.effectiveBase?.absoluteString ?? settings.serverInput
    }

    /// 一眼能认出的写法：host:port（+ 子路径）。完整 URL 放设置页，避免同步页被截断。
    var serverHostText: String {
        let base = settings.effectiveBase ?? settings.resolvedBase
        guard let base else { return settings.serverInput }
        var text = base.host ?? settings.serverInput
        if let port = base.port { text += ":\(port)" }
        let path = base.path
        if !path.isEmpty, path != "/" { text += path }
        return text
    }

    /// 服务器卡上的目标库标签（和「服务器总资产」形成对照）
    var serverLibraryLabel: String {
        settings.library == "material" ? "服务器素材" : "服务器相册"
    }

    /// 目标库在服务器上的张数
    var serverLibraryCount: Int {
        guard let stats = remoteStats else { return 0 }
        return settings.library == "material" ? stats.material : stats.gallery
    }

    /// 服务器总张数
    var serverTotalCount: Int { remoteStats?.count ?? 0 }

    /// 服务器总体积
    var serverBytesText: String {
        guard let stats = remoteStats else { return "—" }
        return Format.bytes(stats.bytes)
    }

    /// 是否已经确认过服务器（曾经连上过，或当前在线）
    var hasServer: Bool { settings.effectiveBase != nil || health.isOnline }

    var isServerOffline: Bool {
        if case .offline = health { return true }
        return false
    }

    var serverErrorText: String? {
        if case .offline(let text) = health { return text }
        return nil
    }

    func goToSettings() { selectedTab = .settings }
    func goToLibrary() { selectedTab = .library }
    func goToSync() { selectedTab = .sync }
}
