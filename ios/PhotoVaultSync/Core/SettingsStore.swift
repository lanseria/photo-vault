//
//  SettingsStore.swift
//  PhotoVaultSync
//
//  App 的全部持久化设置：服务器地址、目标库、同步选项、上次同步时间。
//  地址的「已连通」结果单独存 effectiveBase —— 用户填 bmc_desktop 连不上时，
//  连接测试会把 .local 变体试一遍，成功的那条被记住，下次直接用。
//

import Foundation
import Observation

@MainActor
@Observable
final class SettingsStore {
    private let defaults: UserDefaults

    /// 用户填的地址（原样保留，方便改）
    var serverInput: String {
        didSet { defaults.set(serverInput, forKey: SettingsKeys.serverInput) }
    }

    /// 连接测试确认可用的 base URL（含尾部 /）
    private(set) var effectiveBase: URL?

    /// 目标库：gallery（相册）| material（素材库）
    var library: String {
        didSet { defaults.set(library, forKey: SettingsKeys.library) }
    }

    var options: SyncOptions {
        didSet { persistOptions() }
    }

    private(set) var lastSyncAt: Date?

    /// 正在做连接测试（UI 显示转圈）
    private(set) var isProbing = false

    /// 最近一次连接测试的失败原因（UI 用来显示红色提示）
    private(set) var lastProbeError: ServerError?

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.serverInput = defaults.string(forKey: SettingsKeys.serverInput) ?? ServerEndpoint.defaultAddress
        self.library = defaults.string(forKey: SettingsKeys.library) ?? "gallery"
        if let raw = defaults.string(forKey: SettingsKeys.effectiveBase) {
            self.effectiveBase = URL(string: raw)
        }
        if let data = defaults.data(forKey: SettingsKeys.options),
           let decoded = try? JSONDecoder().decode(SyncOptions.self, from: data) {
            self.options = decoded
        } else {
            self.options = .default
        }
        if let t = defaults.object(forKey: SettingsKeys.lastSyncAt) as? Double, t > 0 {
            self.lastSyncAt = Date(timeIntervalSince1970: t)
        }
    }

    // MARK: - 客户端

    /// 当前可用的 base：优先用连接测试确认过的，否则用输入框规范化结果
    var resolvedBase: URL? {
        effectiveBase ?? ServerEndpoint.normalize(serverInput)
    }

    func makeClient() throws -> APIClient {
        guard let base = resolvedBase else {
            throw ServerError.invalidAddress(serverInput)
        }
        return APIClient(baseURL: base)
    }

    // MARK: - 连接测试

    /// 依次尝试候选地址（含 `<host>.local`），第一个成功的记为 effectiveBase。
    @discardableResult
    func testConnection() async -> Result<RemoteStats, ServerError> {
        guard !isProbing else { return .failure(.transport(message: "正在测试连接，请稍候", hint: nil)) }
        let candidates = ServerEndpoint.candidates(for: serverInput)
        guard !candidates.isEmpty else {
            let error = ServerError.invalidAddress(serverInput)
            lastProbeError = error
            AppLog.shared.add(.error, "地址无效：\(serverInput)")
            return .failure(error)
        }
        isProbing = true
        defer { isProbing = false }

        var lastError: ServerError = .invalidAddress(serverInput)
        for (index, base) in candidates.enumerated() {
            let label = index == 0 ? "首选" : "候选"
            AppLog.shared.add(.info, "连接测试（\(label)）：\(base.absoluteString)")
            do {
                let client = APIClient(baseURL: base, timeout: 6)
                let stats = try await client.stats()
                effectiveBase = base
                lastProbeError = nil
                defaults.set(base.absoluteString, forKey: SettingsKeys.effectiveBase)
                AppLog.shared.add(.success, "已连接 \(base.absoluteString) · \(stats.count) 张")
                return .success(stats)
            } catch let error as ServerError {
                lastError = error
                AppLog.shared.add(.warn, "\(base.absoluteString) 失败：\(error.localizedDescription)")
            } catch {
                lastError = .transport(message: error.localizedDescription, hint: nil)
            }
        }
        lastProbeError = lastError
        return .failure(lastError)
    }

    /// 启动时静默探测。
    ///
    /// 首次启动时 effectiveBase 还是空的，但输入框里已经有默认地址 ——
    /// 这里直接用规范化后的地址试一次，连上就记下来。
    /// 这样 App 一打开就是「在线」，不用先点一次连接测试；也让本地网络权限弹窗
    /// 在用户第一次真正操作前就出现（用户知道它在干嘛）。
    @discardableResult
    func silentProbe() async -> RemoteStats? {
        let candidate = effectiveBase ?? ServerEndpoint.normalize(serverInput)
        guard let base = candidate else { return nil }
        do {
            let stats = try await APIClient(baseURL: base, timeout: 4).stats()
            lastProbeError = nil
            if effectiveBase == nil {
                effectiveBase = base
                defaults.set(base.absoluteString, forKey: SettingsKeys.effectiveBase)
                AppLog.shared.add(.success, "自动连接 \(base.absoluteString) · \(stats.count) 张")
            }
            return stats
        } catch let error as ServerError {
            lastProbeError = error
            return nil
        } catch {
            return nil
        }
    }

    func noteSyncFinished(at date: Date = Date()) {
        lastSyncAt = date
        defaults.set(date.timeIntervalSince1970, forKey: SettingsKeys.lastSyncAt)
    }

    func resetServer() {
        effectiveBase = nil
        defaults.removeObject(forKey: SettingsKeys.effectiveBase)
        lastProbeError = nil
    }

    // MARK: - 持久化

    private func persistOptions() {
        if let data = try? JSONEncoder().encode(options) {
            defaults.set(data, forKey: SettingsKeys.options)
        }
    }

    /// 目标库的中文名
    var libraryTitle: String { library == "material" ? "素材库" : "相册" }
}
