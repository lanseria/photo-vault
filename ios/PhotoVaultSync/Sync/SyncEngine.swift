//
//  SyncEngine.swift
//  PhotoVaultSync
//
//  同步引擎：扫描本机相册 → 规划上传 → 导出原件 → 上传 → 记账。
//
//  公开 API 是跟 UI 的契约（state / stats / recent / authorizationStatus /
//  start / pause / resume / stop / retryFailed / refreshLocalStats / clearFinishedRecords /
//  bootstrap / handleForeground / handleBackground），UI 只读这些；内部实现分布在
//  Sync/ 下其它文件：
//    * PhotoLibrary.swift  —— PhotoKit（授权 / 枚举 / 导出）
//    * SyncPlanner.swift   —— 纯规划（文件名、Live Photo 配对、资源选择）
//    * ServerIndex.swift   —— 服务器索引对账
//    * SyncLedger.swift    —— 账本（增量、断点、对账证据）
//    * UploadClient.swift  —— 后台上传
//    * RetryPolicy.swift   —— 失败分类与退避
//
//  进度语义（UI 可以直接照着显示）：
//    state.total / completed / succeeded / skipped / failed —— 以「一个 PHAsset 一条」计数，
//      Live Photo 的两条文件算同一条；
//    state.bytesSent —— 实际从本机读出来发出去的字节数；
//    recent —— 最近处理的条目，**最新的在最前面**，最多 200 条；
//    stats —— 整库视角（本机有多少 / 已同步多少 / 待同步多少）。
//

import Foundation
import Observation

@MainActor
@Observable
final class SyncEngine {
    // MARK: - 对外状态

    /// 当前这一轮的进度
    private(set) var state: SyncState = SyncState()
    /// 本机相册 + 账本的汇总（同步页顶部三个数字）
    private(set) var stats: LocalLibraryStats = .empty
    /// 最近处理的条目（最新的在最前面，上限 200 条）
    private(set) var recent: [SyncItem] = []
    /// 相册授权状态
    private(set) var authorizationStatus: PhotoAuthorizationStatus = .unknown

    let settings: SettingsStore

    // MARK: - 依赖

    private let library: PhotoLibraryProviding
    private let uploader: UploadTransferring
    private let ledgerFile: LedgerFile
    private var ledger = SyncLedger()

    // MARK: - 本轮运行状态

    private enum Limits {
        static let recentLimit = 200
        /// 每处理 N 条落盘一次；另加时间下限，避免 2 万条时疯狂写 2MB JSON
        static let ledgerSaveEvery = 8
        static let ledgerSaveInterval: TimeInterval = 2
        static let maxWorkers = 6
    }

    private enum RunScope {
        case full
        case retryFailed(Set<String>)
    }

    /// 本轮剩下的上传组（按拍摄时间升序）
    private var queue: [UploadGroup] = []
    private var queueIndex = 0
    private var runBaseURL: URL?
    private var runTask: Task<Void, Never>?
    private var runToken = UUID()
    private var pauseRequested = false
    private var stopRequested = false
    private var processedSinceSave = 0
    private var lastSaveAt = Date.distantPast
    private var isBootstrapped = false

    /// DEBUG 启动参数覆盖（Release 里恒为空，行为不受影响）。测试可以走 init 注入。
    private var debugOverrides = DebugOverrides()
    /// 启动参数解析失败的原因：非 nil 时**拒绝开始同步**，绝不静默降级成「全量上传」
    private var debugOverrideFailure: String?

    init(settings: SettingsStore,
         library: PhotoLibraryProviding? = nil,
         uploader: UploadTransferring? = nil,
         ledgerFile: LedgerFile? = nil,
         debugOverrides: DebugOverrides? = nil,
         debugOverrideFailure: String? = nil) {
        self.settings = settings
        self.library = library ?? SystemPhotoLibrary()
        self.uploader = uploader ?? UploadClient.shared
        self.ledgerFile = ledgerFile ?? LedgerFile(url: LedgerFile.defaultURL())
        #if DEBUG
        if let debugOverrides {
            self.debugOverrides = debugOverrides
            self.debugOverrideFailure = debugOverrideFailure
        } else {
            do {
                self.debugOverrides = try DebugOverrides.parse()
            } catch {
                self.debugOverrideFailure = error.localizedDescription
            }
        }
        #else
        _ = debugOverrides
        _ = debugOverrideFailure
        #endif
    }

    // MARK: - 派生状态

    var isBusy: Bool {
        state.phase == .scanning || state.phase == .syncing
    }

    var canStart: Bool {
        authorizationStatus.isUsable && !isBusy && state.phase != .paused
    }

    var canPause: Bool {
        state.phase == .scanning || state.phase == .syncing
    }

    var canResume: Bool {
        state.phase == .paused
    }

    // MARK: - 生命周期

    /// 冷启动：加载账本、刷新本机统计、接上后台任务与上传会话
    func bootstrap() async {
        guard !isBootstrapped else { return }
        isBootstrapped = true

        #if DEBUG
        applyDebugOverrides()
        #endif
        authorizationStatus = library.authorizationStatus()
        ledger = ledgerFile.load()
        // 上次进程死在「正在上传」的条目回落成 pending，下一轮重新规划
        if ledger.markInterruptedUploadsPending() { persistLedger(force: true) }
        BackgroundSyncManager.shared.attach(engine: self)
        AppLog.shared.add(.info, "同步引擎就绪：账本 \(ledger.records.count) 条 · 相册授权 \(authorizationStatus.title)")

        await refreshLocalStats()

        #if DEBUG
        // QA 自动化钩子：`simctl launch ... -autostartSync` 时启动完直接跑一轮，
        // 不需要点 UI。仅 DEBUG 生效，Release 构建永远不会自动上传。
        // 参数解析失败时绝不自动开始（否则等于「参数没生效 → 全量上传」）。
        if BackgroundSyncManager.shared.autostartSync {
            if let failure = debugOverrideFailure {
                AppLog.shared.add(.error, "启动参数错误，拒绝自动同步：\(failure)")
            } else {
                AppLog.shared.add(.info, "启动参数 -autostartSync：自动开始一轮同步")
                await start()
            }
        }
        #endif
    }

    #if DEBUG
    /// 把 DebugOverrides 应用到本轮设置并打印实际生效值。
    /// 出问题排查第一步就是看这几行日志：fromDate / baseURL / 扫描条数 / 规划条数。
    private func applyDebugOverrides() {
        guard debugOverrideFailure == nil else {
            AppLog.shared.add(.error, "启动参数解析失败：\(debugOverrideFailure ?? "")（本轮拒绝同步，避免误传）")
            return
        }
        guard !debugOverrides.isEmpty else { return }
        AppLog.shared.add(.info, "调试参数生效：\(debugOverrides.summary)")
        if let fromDate = debugOverrides.fromDate {
            settings.options.fromDate = fromDate
        }
        if let raw = debugOverrides.baseURL {
            settings.serverInput = raw
            settings.resetServer()
        }
    }
    #endif

    /// 回前台：授权可能变了、后台会话可能已经落盘新条目
    func handleForeground() async {
        let status = library.authorizationStatus()
        if status != authorizationStatus {
            authorizationStatus = status
            AppLog.shared.add(.info, "相册授权变为 \(status.title)")
        }
        if runTask == nil {
            // 没有跑批时以盘上账本为准（后台 URLSession 在挂起期间完成的条目都已落盘）
            ledger = ledgerFile.load()
            if ledger.markInterruptedUploadsPending() { persistLedger(force: true) }
            await refreshLocalStats()
        }
        BackgroundSyncManager.shared.scheduleAppRefresh()
    }

    /// 进后台：账本落盘 + 申请后台处理时间；在途上传交给后台 URLSession 继续
    func handleBackground() async {
        persistLedger(force: true)
        BackgroundSyncManager.shared.scheduleBackgroundSync()
        if isBusy {
            state.message = "已进入后台：后台 URLSession 会继续把在途上传跑完"
        }
    }

    // MARK: - 授权

    func requestAuthorization() async {
        authorizationStatus = await library.requestAuthorization()
        AppLog.shared.add(authorizationStatus.isUsable ? .success : .warn,
                          "相册授权：\(authorizationStatus.title)")
        if authorizationStatus.isUsable { await refreshLocalStats() }
    }

    /// 部分授权时，让用户重新勾选可见照片
    func presentLimitedLibraryPicker() async {
        await library.presentLimitedLibraryPicker()
        authorizationStatus = library.authorizationStatus()
        await refreshLocalStats()
    }

    // MARK: - 统计

    /// 重新统计「本机有多少、已同步多少、待同步多少」
    func refreshLocalStats() async {
        guard authorizationStatus.isUsable else {
            stats = LocalLibraryStats(total: 0, synced: 0, failed: 0, pending: 0,
                                      bytesUploaded: 0, lastSyncAt: settings.lastSyncAt)
            return
        }
        let assets = await library.fetchAssets(matching: PhotoQuery(options: settings.options))
        let counts = ledger.statistics(for: Set(assets.map(\.id)), library: settings.library)
        stats = LocalLibraryStats(total: assets.count,
                                  synced: counts.synced,
                                  failed: counts.failed,
                                  pending: max(0, assets.count - counts.synced - counts.failed),
                                  bytesUploaded: counts.bytes,
                                  lastSyncAt: settings.lastSyncAt)
    }

    // MARK: - 同步控制

    /// 从头跑一轮：扫描 → 上传。暂停中调用等价于 resume。
    func start() async {
        if let failure = debugOverrideFailure {
            // 参数写错时宁可什么都不传：全量上传的代价远大于这轮失败
            finishFailed(message: "启动参数解析失败，已拒绝上传：\(failure)")
            return
        }
        if state.phase == .paused { await resume(); return }
        guard runTask == nil, !isBusy else { return }
        await beginRunTask { [weak self] in
            guard let self else { return }
            await self.runCycle(scope: .full)
        }
    }

    /// 暂停：不再发新任务，在途的（一个 Live Photo 的两条）会做完
    func pause() {
        guard canPause else { return }
        pauseRequested = true
        state.message = "正在暂停：等当前在途的上传完成…"
    }

    /// 继续暂停的这一轮
    func resume() async {
        guard state.phase == .paused, runTask == nil else { return }
        guard queueIndex < queue.count else {
            // 暂停发生在扫描/规划阶段，队列还没建好：重新跑一轮（扫描很快）
            await start()
            return
        }
        pauseRequested = false
        stopRequested = false
        state.phase = .syncing
        state.finishedAt = nil
        state.message = "继续上传…"
        await beginRunTask { [weak self] in
            guard let self else { return }
            await self.driveWorkers()
            await self.conclude()
        }
    }

    /// 取消当前这一轮（已完成的不回滚）
    func stop() async {
        guard state.isActive else { return }
        stopRequested = true
        pauseRequested = false
        state.message = "正在停止…"
        uploader.cancelAll()
        if let task = runTask {
            await task.value
            if runTask != nil { runTask = nil }
        } else {
            // 处于暂停状态时没有 worker 在跑，直接收尾
            clearQueue()
            await conclude()
        }
    }

    /// 只重跑失败队列（不需要重新拉服务器索引）
    func retryFailed() async {
        guard runTask == nil, !state.isActive else { return }
        let failedIDs = ledger.failedAssetIDs
        guard !failedIDs.isEmpty else {
            state.phase = .finished
            state.message = "没有失败的条目"
            state.errorText = nil
            return
        }
        await beginRunTask { [weak self] in
            guard let self else { return }
            await self.runCycle(scope: .retryFailed(failedIDs))
        }
    }

    /// 清掉失败/跳过记录，让它们重新参与下一轮
    func clearFinishedRecords() async {
        guard !state.isActive else { return }
        let removed = ledger.removeFinishedRecords()
        persistLedger(force: true)
        await refreshLocalStats()
        state.message = removed > 0
            ? "已清除 \(removed) 条失败/跳过记录，下一轮会重新检查"
            : "没有可清除的失败/跳过记录"
        state.errorText = nil
        AppLog.shared.add(.info, state.message)
    }

    // MARK: - 一轮的具体流程

    private func beginRunTask(_ body: @escaping @MainActor () async -> Void) async {
        let token = UUID()
        runToken = token
        let task = Task { @MainActor in await body() }
        runTask = task
        await task.value
        if runToken == token { runTask = nil }
    }

    private func runCycle(scope: RunScope) async {
        pauseRequested = false
        stopRequested = false
        queue = []
        queueIndex = 0
        recent.removeAll()
        state = SyncState(phase: .scanning, message: "正在准备…", startedAt: Date())

        guard let baseURL = settings.resolvedBase else {
            finishFailed(message: "还没有设置服务器地址，先去设置页填一个")
            return
        }
        runBaseURL = baseURL

        // 1) 授权（首次进来会弹系统授权框）
        if !authorizationStatus.isUsable {
            authorizationStatus = library.authorizationStatus()
            if authorizationStatus == .notDetermined {
                authorizationStatus = await library.requestAuthorization()
            }
            guard authorizationStatus.isUsable else {
                finishFailed(message: "没有相册访问权限（当前：\(authorizationStatus.title)）")
                return
            }
        }

        // 2) 服务器索引对账（服务器上已有的一律跳过，2 万张相册这是必须的）
        var serverIndex: ServerIndex?
        if settings.options.skipExistingOnServer, case .full = scope {
            state.message = "正在核对服务器已有索引…"
            AppLog.shared.add(.info, "开始拉取服务器索引（\(settings.library)）")
            let client = APIClient(baseURL: baseURL)
            let result = await ServerIndexLoader.load(
                client: client,
                library: settings.library,
                onProgress: { [weak self] fetched, total in
                    guard let self else { return }
                    self.state.message = total > 0
                        ? "正在核对服务器已有索引… \(fetched)/\(total)"
                        : "正在核对服务器已有索引… \(fetched)"
                },
                shouldStop: { [weak self] in
                    guard let self else { return true }
                    return self.stopRequested || self.pauseRequested
                })
            serverIndex = result.index
            if let note = result.degradedNote {
                AppLog.shared.add(.warn, note)
                state.message = note
            } else {
                AppLog.shared.add(.info, "服务器索引 \(result.index.count) 条")
            }
        }
        if stopRequested || pauseRequested {
            await conclude()
            return
        }

        // 3) 扫描本机相册
        let query = PhotoQuery(options: settings.options)
        let fromText = query.fromDate.map { ServerDate.display.string(from: $0) } ?? "不限"
        AppLog.shared.add(.info, "本轮参数：base=\(baseURL.absoluteString) · library=\(settings.library) · fromDate=\(fromText) · 照片=\(query.includePhotos) 视频=\(query.includeVideos) 实况=\(query.includeLivePhotos) 隐藏=\(query.includeHidden)")
        state.message = "正在扫描本机相册…"
        let assets = await library.fetchAssets(matching: query)
        AppLog.shared.add(.info, "相册扫描完成：\(assets.count) 条（按选项过滤后）")
        if stopRequested || pauseRequested {
            await conclude()
            return
        }

        // 4) 规划：过滤 → 增量（账本）→ 服务器索引对账
        state.message = "正在规划上传队列…"
        var groups: [UploadGroup] = []
        var skips: [UploadGroup.ServerSkip] = []
        var inspected = 0
        for asset in assets {
            if stopRequested || pauseRequested || Task.isCancelled { break }
            if case .retryFailed(let ids) = scope, !ids.contains(asset.id) { continue }
            guard !SyncPlanner.isUpToDate(asset: asset, ledger: ledger,
                                          library: settings.library) else {
                inspected += 1
                continue
            }
            let resources = await library.resources(for: asset.id)
            guard let group = SyncPlanner.group(for: asset, resources: resources,
                                                options: settings.options) else { continue }
            if let match = serverIndex?.match(group: group) {
                // 完全匹配（Live Photo 要求服务器上已经是 live_photo）
                skips.append(group.skip(serverAssetID: match.id, serverKind: match.kind))
            } else if group.kind == .livePhoto,
                      let still = serverIndex?.matchStill(of: group) {
                // 服务器上只有这张的静帧（kind=photo），没有实况动态。
                // 此时绝不能只传动态：服务端会把它记成一条独立 video（污染库）。
                // 整组跳过并留下日志，等用户在服务器上处理后用「清除跳过记录」重试。
                AppLog.shared.add(.warn, "\(group.displayName) 服务器已有静帧（kind=\(still.kind)）但没有实况动态，整组跳过以避免产生孤立视频")
                skips.append(group.skip(serverAssetID: still.id, serverKind: still.kind))
            } else {
                groups.append(group)
            }
            inspected += 1
            if inspected % 25 == 0 {
                state.message = "正在规划上传队列… \(inspected)/\(assets.count)"
                await Task.yield()
            }
        }
        let plannedNames = (groups.map(\.displayName) + skips.map(\.name)).sorted()
        AppLog.shared.add(.info, "规划完成：待上传 \(groups.count) 组 · 服务器已有 \(skips.count) 项 · 合计 \(plannedNames.count) 项 · 名单=\(preview(names: plannedNames))")

        // 5) 队列就绪：按拍摄时间升序（先传老的，符合备份直觉）
        groups.sort { lhs, rhs in
            let left = lhs.takenAt ?? .distantPast
            let right = rhs.takenAt ?? .distantPast
            return left != right ? left < right : lhs.assetIdentifier < rhs.assetIdentifier
        }
        queue = groups
        queueIndex = 0
        state.succeeded = 0
        state.skipped = 0
        state.failed = 0
        state.bytesSent = 0
        state.total = groups.count + skips.count

        // 安全网：Debug 的 -expectPlannedCount N 对不上就中止，**绝不进入上传阶段**。
        // 典型场景：想只传 1 条 iostest，结果 fromDate 没生效扫到一堆老素材。
        if let expected = debugOverrides.expectPlannedCount, plannedNames.count != expected {
            await abortForPlannedCountGuard(expected: expected, actual: plannedNames.count,
                                            names: plannedNames)
            return
        }

        // 服务器上已经有的先记跳过（不占 worker，但计入进度，UI 数字才自洽）
        for skip in skips {
            ledger.markSkipped(assetID: skip.assetIdentifier, name: skip.name,
                               modificationDate: skip.modificationDate,
                               serverAssetID: skip.serverAssetID,
                               library: settings.library)
            insertRecent(SyncItem(id: skip.assetIdentifier,
                                  assetIdentifier: skip.assetIdentifier,
                                  name: skip.name,
                                  state: .skipped))
            state.skipped += 1
        }
        state.completed = state.succeeded + state.skipped + state.failed
        persistLedger(force: true)

        if stopRequested || pauseRequested || queue.isEmpty {
            await conclude()
            return
        }

        // 6) 并发上传。一个 Live Photo 的两条文件在同一个 worker 里串行处理，
        //    所以 concurrentUploads 控制的是「同时处理几个 PHAsset」，配对不会被打散。
        let workerCount = max(1, min(settings.options.concurrentUploads, Limits.maxWorkers))
        state.phase = .syncing
        state.message = "开始上传 \(queue.count) 项（并发 \(workerCount)）"
        await driveWorkers()
        await conclude()
    }

    // MARK: - worker

    private func driveWorkers() async {
        let workerCount = max(1, min(settings.options.concurrentUploads, Limits.maxWorkers))
        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<workerCount {
                group.addTask { @MainActor [weak self] in
                    guard let self else { return }
                    await self.workerLoop()
                }
            }
        }
    }

    private func workerLoop() async {
        while true {
            if stopRequested || pauseRequested || Task.isCancelled { return }
            guard let group = takeNextGroup() else { return }
            await process(group)
        }
    }

    private func takeNextGroup() -> UploadGroup? {
        guard queueIndex < queue.count else { return nil }
        let group = queue[queueIndex]
        queueIndex += 1
        return group
    }

    // MARK: - 单条处理

    private struct UploadOutcome {
        var response: UploadResponse
        var bytes: Int64
    }

    private func process(_ group: UploadGroup) async {
        let recentID = group.assetIdentifier
        var sentBytes: Int64 = 0
        var lastResponse: UploadResponse?

        state.currentName = group.displayName
        insertRecent(SyncItem(id: recentID,
                              assetIdentifier: group.assetIdentifier,
                              name: group.displayName,
                              state: .uploading))
        ledger.markUploading(assetID: group.assetIdentifier,
                             name: group.displayName,
                             modificationDate: group.modificationDate,
                             library: settings.library)

        do {
            // Live Photo：files 里静帧在前、动态在后，这里天然串行
            for file in group.files {
                if stopRequested || Task.isCancelled { throw UploadError.cancelled }
                state.currentName = file.name
                let outcome = try await exportAndUpload(file, assetID: group.assetIdentifier,
                                                        takenAt: group.takenAt)
                sentBytes += outcome.bytes
                lastResponse = outcome.response
            }
            ledger.markSynced(assetID: group.assetIdentifier,
                              name: group.displayName,
                              modificationDate: group.modificationDate,
                              serverAssetID: lastResponse?.asset?.id
                                  ?? ledger.entry(forAsset: group.assetIdentifier)?.serverAssetID,
                              bytes: sentBytes,
                              library: settings.library)
            state.succeeded += 1
            state.bytesSent += sentBytes
            updateRecent(id: recentID) { item in
                item.state = .done
                item.bytes = sentBytes
                item.error = nil
            }
            let kindText = lastResponse?.asset?.kind ?? defaultKindText(for: group)
            let idText = lastResponse?.asset?.id ?? "(响应无 asset id)"
            AppLog.shared.add(.success, "\(group.displayName) → \(kindText) · \(idText)")
        } catch {
            state.currentName = ""
            if RetryPolicy.isCancellation(error) {
                // 停止 / 取消：账本回退成 pending，下一轮会重新规划到它（不算失败）
                ledger.markPending(assetID: group.assetIdentifier)
                updateRecent(id: recentID) { item in
                    item.state = .failed
                    item.error = "已停止"
                }
                return
            }
            let message = describe(error)
            ledger.markFailed(assetID: group.assetIdentifier,
                              name: group.displayName,
                              modificationDate: group.modificationDate,
                              error: message,
                              library: settings.library)
            state.failed += 1
            state.errorText = message
            updateRecent(id: recentID) { item in
                item.state = .failed
                item.error = message
            }
            AppLog.shared.add(.error, "\(group.displayName) 上传失败：\(message)")
        }

        state.completed = state.succeeded + state.skipped + state.failed
        state.message = "正在上传 \(state.completed)/\(state.total) · 已传 \(Format.bytes(state.bytesSent))"
        persistLedger(force: false)
    }

    /// 导出一份 → 上传 → 删临时文件。网络类失败按 RetryPolicy 退避重试。
    /// takenAt 是 PHAsset.creationDate：文件没有内嵌时间时服务端用它，日期才不会变成"现在"。
    private func exportAndUpload(_ file: UploadFilePlan,
                                 assetID: String,
                                 takenAt: Date?) async throws -> UploadOutcome {
        guard let baseURL = runBaseURL else { throw ServerError.notConfigured }
        let allowNetwork = settings.options.allowICloudDownload
        let libraryName = settings.library
        var attempt = 1

        while true {
            if stopRequested || Task.isCancelled { throw UploadError.cancelled }

            let localURL: URL
            do {
                localURL = try await library.exportOriginal(of: assetID,
                                                            resource: file.resource,
                                                            allowNetwork: allowNetwork)
            } catch {
                if stopRequested || Task.isCancelled || RetryPolicy.isCancellation(error) {
                    throw UploadError.cancelled
                }
                if attempt < RetryPolicy.maxAttempts, RetryPolicy.isRetryable(error) {
                    let delay = RetryPolicy.delaySeconds(afterAttempt: attempt)
                    AppLog.shared.add(.warn, "\(file.name) 导出失败（第 \(attempt) 次），\(delay)s 后重试：\(describe(error))")
                    try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                    attempt += 1
                    continue
                }
                throw error
            }

            let bytes = Self.fileSize(localURL)
            do {
                let response = try await uploader.upload(fileURL: localURL,
                                                         name: file.name,
                                                         library: libraryName,
                                                         batch: file.batchID,
                                                         takenAt: takenAt,
                                                         baseURL: baseURL)
                try? FileManager.default.removeItem(at: localURL)
                return UploadOutcome(response: response, bytes: bytes)
            } catch {
                try? FileManager.default.removeItem(at: localURL)
                if stopRequested || Task.isCancelled || RetryPolicy.isCancellation(error) {
                    throw UploadError.cancelled
                }
                if attempt < RetryPolicy.maxAttempts, RetryPolicy.isRetryable(error) {
                    let delay = RetryPolicy.delaySeconds(afterAttempt: attempt)
                    AppLog.shared.add(.warn, "\(file.name) 上传失败（第 \(attempt) 次），\(delay)s 后重试：\(describe(error))")
                    try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                    attempt += 1
                    continue
                }
                throw error
            }
        }
    }

    // MARK: - 收尾

    private func conclude() async {
        persistLedger(force: true)
        if stopRequested {
            clearQueue()
            finishStopped()
        } else if pauseRequested {
            finishPaused()
        } else {
            await finishCompleted()
        }
    }

    private func finishStopped() {
        state.phase = .finished
        state.finishedAt = Date()
        state.currentName = ""
        state.completed = state.succeeded + state.skipped + state.failed
        state.errorText = state.failed > 0 ? "\(state.failed) 项失败，可点「重试失败」" : nil
        state.message = "已停止：成功 \(state.succeeded) · 跳过 \(state.skipped) · 失败 \(state.failed)"
        AppLog.shared.add(.warn, state.message)
    }

    private func finishPaused() {
        state.phase = .paused
        state.finishedAt = Date()
        state.currentName = ""
        state.completed = state.succeeded + state.skipped + state.failed
        state.message = "已暂停（已完成 \(state.completed)/\(max(state.total, state.completed))）"
        AppLog.shared.add(.info, state.message)
    }

    private func finishCompleted() async {
        clearQueue()
        state.phase = .finished
        state.finishedAt = Date()
        state.currentName = ""
        state.completed = state.succeeded + state.skipped + state.failed
        if state.failed > 0 {
            state.errorText = "\(state.failed) 项失败，可点「重试失败」"
        } else {
            state.errorText = nil
        }
        if state.total == 0 {
            state.message = "本机没有需要同步的新内容"
        } else {
            state.message = "同步完成：成功 \(state.succeeded) · 跳过 \(state.skipped) · 失败 \(state.failed) · \(Format.bytes(state.bytesSent))"
        }
        if state.succeeded > 0 { settings.noteSyncFinished() }
        AppLog.shared.add(state.failed > 0 ? .warn : .success, state.message)
        await refreshLocalStats()
    }

    private func finishFailed(message: String) {
        clearQueue()
        state.phase = .failed
        state.finishedAt = Date()
        state.currentName = ""
        state.message = message
        state.errorText = message
        AppLog.shared.add(.error, message)
    }

    /// -expectPlannedCount 对不上：中止本轮。账本不记录这些资产（避免污染），也不发任何请求。
    private func abortForPlannedCountGuard(expected: Int, actual: Int, names: [String]) async {
        clearQueue()
        state.phase = .failed
        state.finishedAt = Date()
        state.total = 0
        state.completed = 0
        state.succeeded = 0
        state.skipped = 0
        state.failed = 0
        let message = "安全检查未通过：期望规划 \(expected) 项，实际 \(actual) 项，已中止（未上传任何文件）。实际名单：\(names.joined(separator: ", "))"
        state.message = message
        state.errorText = message
        AppLog.shared.add(.error, message)
        await refreshLocalStats()
    }

    private func preview(names: [String], limit: Int = 40) -> String {
        if names.isEmpty { return "(空)" }
        if names.count <= limit { return names.joined(separator: ", ") }
        return names.prefix(limit).joined(separator: ", ") + " …(+\\(names.count - limit))"
    }

    private func clearQueue() {
        queue = []
        queueIndex = 0
        state.currentName = ""
    }

    // MARK: - 账本持久化

    private func persistLedger(force: Bool) {
        processedSinceSave += 1
        let now = Date()
        let due = force
            || processedSinceSave >= Limits.ledgerSaveEvery
            || now.timeIntervalSince(lastSaveAt) >= Limits.ledgerSaveInterval
        guard due else { return }
        do {
            try ledgerFile.save(ledger)
            processedSinceSave = 0
            lastSaveAt = now
        } catch {
            AppLog.shared.add(.warn, "账本写入失败：\(error.localizedDescription)")
        }
    }

    // MARK: - recent 列表

    private func insertRecent(_ item: SyncItem) {
        recent.insert(item, at: 0)
        if recent.count > Limits.recentLimit {
            recent.removeLast(recent.count - Limits.recentLimit)
        }
    }

    private func updateRecent(id: String, mutate: (inout SyncItem) -> Void) {
        guard let index = recent.firstIndex(where: { $0.id == id }) else { return }
        var item = recent[index]
        mutate(&item)
        item.updatedAt = Date()
        recent[index] = item
    }

    // MARK: - 小工具

    private func defaultKindText(for group: UploadGroup) -> String {
        switch group.kind {
        case .photo: return "photo"
        case .video: return "video"
        case .livePhoto: return "live_photo"
        }
    }

    private func describe(_ error: Error) -> String {
        if let upload = error as? UploadError { return upload.localizedDescription }
        if let photo = error as? PhotoLibraryError { return photo.localizedDescription }
        if let server = error as? ServerError { return server.localizedDescription }
        return error.localizedDescription
    }

    private static func fileSize(_ url: URL) -> Int64 {
        (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int64) ?? 0
    }
}
