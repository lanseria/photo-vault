//
//  BackgroundSync.swift
//  PhotoVaultSync
//
//  BGTaskScheduler 接线：
//    * com.photovault.ios.sync     —— 后台处理任务（processing），进后台后排一次，
//                                     系统挑合适时机唤醒 App 继续同步；
//    * com.photovault.ios.refresh  —— 短时刷新，只做「回前台前的状态刷新」。
//
//  注册必须发生在 didFinishLaunching 返回之前，所以由 AppDelegate 调用 registerTasks()；
//  引擎实例在 SyncEngine.bootstrap() 里 attach 进来（那时 UI 已经起来了）。
//  模拟器不支持后台处理调度（submit 返回 unavailable），失败静默忽略，不影响前台同步。
//

import BackgroundTasks
import Foundation
import UIKit

final class BackgroundSyncManager: @unchecked Sendable {
    static let shared = BackgroundSyncManager()

    static let syncTaskIdentifier = "com.photovault.ios.sync"
    static let refreshTaskIdentifier = "com.photovault.ios.refresh"

    private let lock = NSLock()
    private weak var engine: SyncEngine?
    private var tasksRegistered = false

    /// QA 自动化钩子：`simctl launch ... -autostartSync` 时引擎启动后自动跑一轮（仅 DEBUG 使用）
    let autostartSync: Bool = ProcessInfo.processInfo.arguments.contains("-autostartSync")

    private init() {}

    // MARK: - 接线

    func attach(engine: SyncEngine) {
        lock.lock()
        self.engine = engine
        lock.unlock()
    }

    /// AppDelegate.didFinishLaunching 里调用（BGTaskScheduler 要求启动结束前注册完）
    func registerTasks() {
        lock.lock()
        if tasksRegistered {
            lock.unlock()
            return
        }
        tasksRegistered = true
        lock.unlock()

        // 注册失败（Info.plist 没配 identifier / 重复注册）只记日志，不影响 App 启动
        let registeredSync = BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.syncTaskIdentifier, using: nil
        ) { [weak self] task in
            guard let self, let processing = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self.handle(processing: processing)
        }

        let registeredRefresh = BGTaskScheduler.shared.register(
            forTaskWithIdentifier: Self.refreshTaskIdentifier, using: nil
        ) { [weak self] task in
            guard let self, let refresh = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            self.handle(appRefresh: refresh)
        }

        Task { @MainActor in
            AppLog.shared.add(.info, "后台任务注册：sync=\(registeredSync) refresh=\(registeredRefresh)")
        }
    }

    // MARK: - 调度

    /// 进后台时排一次后台同步任务
    func scheduleBackgroundSync() {
        let request = BGProcessingTaskRequest(identifier: Self.syncTaskIdentifier)
        request.requiresNetworkConnectivity = true
        request.requiresExternalPower = false
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // 模拟器 / 用户关了后台刷新都会走到这里；不打扰用户，只在调试日志里留痕
            Task { @MainActor in
                AppLog.shared.add(.info, "后台同步任务排队失败（模拟器正常现象）：\(error.localizedDescription)")
            }
        }
    }

    /// 轻量刷新，让下次回前台时状态更新鲜
    func scheduleAppRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTaskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    // MARK: - 任务执行

    private func engineSnapshot() -> SyncEngine? {
        lock.lock()
        defer { lock.unlock() }
        return engine
    }

    private func handle(processing task: BGProcessingTask) {
        // 先把下一次排上，否则跑完这次就再也不会被调度
        scheduleBackgroundSync()
        Task { @MainActor [weak self] in
            guard let engine = self?.engineSnapshot() else {
                task.setTaskCompleted(success: false)
                return
            }
            // 系统收走时间就立刻停：至少把账本落盘、在途任务取消
            task.expirationHandler = {
                Task { @MainActor in await engine.stop() }
            }
            await engine.start()
            task.setTaskCompleted(success: true)
        }
    }

    private func handle(appRefresh task: BGAppRefreshTask) {
        scheduleAppRefresh()
        Task { @MainActor [weak self] in
            guard let engine = self?.engineSnapshot() else {
                task.setTaskCompleted(success: false)
                return
            }
            task.expirationHandler = {}
            await engine.handleForeground()
            task.setTaskCompleted(success: true)
        }
    }
}
