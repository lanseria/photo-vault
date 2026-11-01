//
//  SyncEngineTests.swift
//  PhotoVaultSyncTests
//
//  引擎编排单测：用假相册 + 假上传器跑完整 start() 流程，验证
//    * 上传顺序（Live Photo 先静帧后动态、同 batch）
//    * 账本落盘（serverAssetID / bytes / 增量跳过）
//    * 选项过滤（只传静帧 / fromDate）
//    * retryFailed 只重跑失败条目
//  不连网络、不碰真相册。
//

import XCTest
@testable import PhotoVaultSync

@MainActor
final class SyncEngineTests: XCTestCase {

    private var suiteName: String!
    private var defaults: UserDefaults!
    private var directory: URL!

    override func setUp() {
        super.setUp()
        suiteName = "SyncEngineTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("sync-engine-tests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() {
        if let suiteName { defaults?.removePersistentDomain(forName: suiteName) }
        try? FileManager.default.removeItem(at: directory)
        defaults = nil
        directory = nil
        suiteName = nil
        super.tearDown()
    }

    // MARK: - 假实现

    @MainActor
    private final class FakePhotoLibrary: PhotoLibraryProviding {
        var assets: [PhotoAssetInfo] = []
        var resourceMap: [String: [PhotoResourceInfo]] = [:]
        var exportCount = 0

        func authorizationStatus() -> PhotoAuthorizationStatus { .authorized }
        func requestAuthorization() async -> PhotoAuthorizationStatus { .authorized }
        func presentLimitedLibraryPicker() async {}

        func fetchAssets(matching query: PhotoQuery) async -> [PhotoAssetInfo] {
            assets.filter { query.accepts($0) }
        }

        func resources(for assetID: String) async -> [PhotoResourceInfo] {
            resourceMap[assetID] ?? []
        }

        func exportOriginal(of assetID: String, resource: PhotoResourceInfo,
                            allowNetwork: Bool) async throws -> URL {
            exportCount += 1
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("\(UUID().uuidString).\(resource.kind.rawValue)")
            try Data(repeating: 7, count: 32).write(to: url)
            return url
        }
    }

    @MainActor
    private final class FakeUploader: UploadTransferring {
        struct Call: Equatable {
            var name: String
            var batch: String
            var library: String
            /// 引擎随请求带上的拍摄时间（服务端契约：taken）
            var takenAt: Date?
        }

        var calls: [Call] = []
        /// 某个文件名第一次上传时故意失败（模拟网络抖动）
        var failingOnce: [String: Error] = [:]

        func upload(fileURL: URL, name: String, library: String, batch: String,
                    takenAt: Date?, baseURL: URL) async throws -> UploadResponse {
            calls.append(Call(name: name, batch: batch, library: library, takenAt: takenAt))
            if let error = failingOnce.removeValue(forKey: name) { throw error }
            var response = UploadResponse()
            response.status = "imported"
            var serverAsset = RemoteAsset()
            serverAsset.id = "server-\(name)"
            serverAsset.kind = name.uppercased().hasSuffix(".MOV") ? "live_photo" : "photo"
            response.asset = serverAsset
            response.bytes = 32
            return response
        }

        func cancelAll() {}
    }

    // MARK: - 工具

    private func asset(id: String, media: PhotoMediaType = .photo, live: Bool = false,
                       created: TimeInterval, width: Int = 640, height: Int = 480,
                       duration: TimeInterval = 0) -> PhotoAssetInfo {
        PhotoAssetInfo(id: id, mediaType: media,
                       creationDate: Date(timeIntervalSince1970: created),
                       modificationDate: Date(timeIntervalSince1970: created),
                       isLivePhoto: live,
                       pixelWidth: width, pixelHeight: height,
                       duration: duration)
    }

    private func makeEngine(library: FakePhotoLibrary,
                            uploader: FakeUploader,
                            configure: (SettingsStore) -> Void = { _ in },
                            debugOverrides: DebugOverrides? = nil,
                            debugOverrideFailure: String? = nil) -> SyncEngine {
        let store = SettingsStore(defaults: defaults)
        store.serverInput = "http://127.0.0.1:9/"
        store.options.skipExistingOnServer = false   // 单测不打服务器
        store.options.concurrentUploads = 2
        configure(store)
        return SyncEngine(settings: store,
                          library: library,
                          uploader: uploader,
                          ledgerFile: LedgerFile(url: directory.appendingPathComponent("SyncLedger.json")),
                          debugOverrides: debugOverrides,
                          debugOverrideFailure: debugOverrideFailure)
    }

    private func standardLibrary() -> FakePhotoLibrary {
        let library = FakePhotoLibrary()
        library.assets = [
            asset(id: "PHOTO-1", created: 1_000),
            asset(id: "LIVE-1", live: true, created: 2_000, width: 4032, height: 3024),
        ]
        library.resourceMap = [
            "PHOTO-1": [PhotoResourceInfo(kind: .photo, originalFilename: "iostest-a.jpg",
                                          uniformTypeIdentifier: "public.jpeg")],
            "LIVE-1": [
                PhotoResourceInfo(kind: .photo, originalFilename: "iostest-livep.HEIC",
                                  uniformTypeIdentifier: "public.heic"),
                PhotoResourceInfo(kind: .pairedVideo, originalFilename: "iostest-livep.MOV",
                                  uniformTypeIdentifier: "com.apple.quicktime-movie"),
            ],
        ]
        return library
    }

    private func loadLedger() -> SyncLedger {
        LedgerFile(url: directory.appendingPathComponent("SyncLedger.json")).load()
    }

    // MARK: - 完整一轮

    func testFullRunUploadsPhotoAndLivePhotoInOrder() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.start()

        XCTAssertEqual(engine.state.phase, .finished)
        XCTAssertEqual(engine.state.total, 2)
        XCTAssertEqual(engine.state.succeeded, 2)
        XCTAssertEqual(engine.state.skipped, 0)
        XCTAssertEqual(engine.state.failed, 0)
        XCTAssertEqual(engine.state.bytesSent, 96)   // 2 个文件 + Live Photo 2 个文件，每个 32B

        // Live Photo：静帧在前、动态在后，同一个 batch
        let names = uploader.calls.map(\.name)
        XCTAssertEqual(names.filter { $0 == "iostest-a.jpg" }.count, 1)
        let stillIndex = try XCTUnwrap(names.firstIndex(of: "iostest-livep.HEIC"))
        let motionIndex = try XCTUnwrap(names.firstIndex(of: "iostest-livep.MOV"))
        XCTAssertLessThan(stillIndex, motionIndex, "Live Photo 必须先传静帧再传动态")
        XCTAssertEqual(uploader.calls[stillIndex].batch, uploader.calls[motionIndex].batch)
        XCTAssertNotEqual(uploader.calls[stillIndex].batch, uploader.calls.first { $0.name == "iostest-a.jpg" }?.batch)
        XCTAssertEqual(uploader.calls[stillIndex].library, "gallery")

        // 拍摄时间要随请求一起发（服务端在文件没有内嵌时间时用它，避免日期变成"上传那一刻"）
        XCTAssertEqual(uploader.calls.first { $0.name == "iostest-a.jpg" }?.takenAt,
                       Date(timeIntervalSince1970: 1_000))
        XCTAssertEqual(uploader.calls.first { $0.name == "iostest-livep.MOV" }?.takenAt,
                       Date(timeIntervalSince1970: 2_000))

        // 账本：done + 服务器 asset id（Live Photo 记合成后动态那条的 id）
        let ledger = loadLedger()
        XCTAssertEqual(ledger.records.count, 2)
        let photoEntry = try XCTUnwrap(ledger.entry(forAsset: "PHOTO-1"))
        XCTAssertEqual(photoEntry.state, .done)
        XCTAssertEqual(photoEntry.serverAssetID, "server-iostest-a.jpg")
        let liveEntry = try XCTUnwrap(ledger.entry(forAsset: "LIVE-1"))
        XCTAssertEqual(liveEntry.state, .done)
        XCTAssertEqual(liveEntry.serverAssetID, "server-iostest-livep.MOV")
        XCTAssertEqual(liveEntry.name, "iostest-livep.HEIC")
        XCTAssertEqual(liveEntry.bytes, 64)

        // 本机统计
        XCTAssertEqual(engine.stats.total, 2)
        XCTAssertEqual(engine.stats.synced, 2)
        XCTAssertEqual(engine.stats.pending, 0)
        XCTAssertEqual(engine.recent.count, 2)
        XCTAssertTrue(engine.recent.allSatisfy { $0.state == .done })
    }

    func testSecondRunSkipsUpToDateAssets() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.start()
        let uploadsAfterFirstRun = uploader.calls.count
        XCTAssertEqual(uploadsAfterFirstRun, 3)

        await engine.start()
        XCTAssertEqual(uploader.calls.count, uploadsAfterFirstRun, "第二轮不该重传")
        XCTAssertEqual(engine.state.phase, .finished)
        XCTAssertEqual(engine.state.total, 0)
        XCTAssertEqual(engine.state.succeeded, 0)
        XCTAssertEqual(engine.stats.synced, 2)
    }

    func testSwitchingTargetLibraryReuploads() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.start()
        let firstRunUploads = uploader.calls.count
        XCTAssertEqual(firstRunUploads, 3)
        XCTAssertEqual(uploader.calls.first?.library, "gallery")

        // 设置里切到素材库：账本是按库记账的，同一张要往新库再传一次
        engine.settings.library = "material"
        await engine.start()

        XCTAssertEqual(uploader.calls.count, firstRunUploads + 3)
        XCTAssertEqual(uploader.calls.last?.library, "material")
        XCTAssertEqual(engine.state.succeeded, 2)
        XCTAssertEqual(loadLedger().entry(forAsset: "PHOTO-1")?.library, "material")
    }

    func testLivePhotoDisabledUploadsStillOnly() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader) { store in
            store.options.includeLivePhotos = false
        }
        await engine.bootstrap()

        await engine.start()

        let names = uploader.calls.map(\.name)
        XCTAssertTrue(names.contains("iostest-livep.HEIC"))
        XCTAssertFalse(names.contains("iostest-livep.MOV"))
        XCTAssertEqual(engine.state.succeeded, 2)
        XCTAssertEqual(loadLedger().entry(forAsset: "LIVE-1")?.state, .done)
    }

    func testFromDateFiltersOutOldAssets() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader) { store in
            store.options.fromDate = Date(timeIntervalSince1970: 1_500)
        }
        await engine.bootstrap()

        await engine.start()

        XCTAssertEqual(uploader.calls.map(\.name).contains("iostest-a.jpg"), false)
        XCTAssertEqual(engine.state.total, 1)
        XCTAssertEqual(engine.state.succeeded, 1)
        XCTAssertEqual(engine.stats.total, 1)
    }

    // MARK: - e2e 安全网（-expectPlannedCount）

    func testExpectPlannedCountMismatchAbortsWithoutUploading() async throws {
        let library = standardLibrary()      // 2 组：1 张照片 + 1 个 Live Photo
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader,
                                debugOverrides: DebugOverrides(expectPlannedCount: 3))
        await engine.bootstrap()

        await engine.start()

        XCTAssertTrue(uploader.calls.isEmpty, "条数对不上时一个文件都不能传")
        XCTAssertEqual(engine.state.phase, .failed)
        XCTAssertEqual(engine.state.total, 0)
        XCTAssertTrue(engine.state.errorText?.contains("安全检查未通过") ?? false)
        XCTAssertTrue(engine.state.errorText?.contains("iostest-a.jpg") ?? false,
                      "错误里要带实际名单，方便一眼看出扫到了什么")
        XCTAssertTrue(loadLedger().records.isEmpty, "被安全网拦下的资产不能记进账本")
    }

    func testExpectPlannedCountMatchProceeds() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader,
                                debugOverrides: DebugOverrides(expectPlannedCount: 2))
        await engine.bootstrap()

        await engine.start()

        XCTAssertEqual(engine.state.phase, .finished)
        XCTAssertEqual(engine.state.succeeded, 2)
        XCTAssertEqual(uploader.calls.count, 3)
    }

    func testDebugOverrideFailureBlocksStart() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        let engine = makeEngine(library: library, uploader: uploader,
                                debugOverrides: DebugOverrides(),
                                debugOverrideFailure: "-syncFromDateEpoch 需要 Unix 秒（例如 1793462400）")
        await engine.bootstrap()

        await engine.start()

        XCTAssertTrue(uploader.calls.isEmpty, "参数写错时绝不能开始上传")
        XCTAssertEqual(engine.state.phase, .failed)
        XCTAssertTrue(engine.state.errorText?.contains("启动参数解析失败") ?? false)
    }

    // MARK: - 重试

    func testNetworkFailureIsRetriedWithinRun() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()
        uploader.failingOnce["iostest-a.jpg"] = UploadError.network(message: "抖动")
        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.start()

        XCTAssertEqual(uploader.calls.filter { $0.name == "iostest-a.jpg" }.count, 2,
                       "网络类失败应该重试一次")
        XCTAssertEqual(engine.state.succeeded, 2)
        XCTAssertEqual(engine.state.failed, 0)
    }

    func testServerClientErrorIsNotRetriedAndRecorded() async throws {
        let library = FakePhotoLibrary()
        library.assets = [asset(id: "PHOTO-1", created: 1_000)]
        library.resourceMap = ["PHOTO-1": [PhotoResourceInfo(kind: .photo, originalFilename: "iostest-a.jpg")]]
        let uploader = FakeUploader()
        uploader.failingOnce["iostest-a.jpg"] = UploadError.server(status: 415, detail: "不支持的文件类型")
        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.start()

        XCTAssertEqual(uploader.calls.count, 1, "4xx 不该重试")
        XCTAssertEqual(engine.state.failed, 1)
        XCTAssertEqual(engine.state.phase, .finished)
        XCTAssertNotNil(engine.state.errorText)
        let entry = try XCTUnwrap(loadLedger().entry(forAsset: "PHOTO-1"))
        XCTAssertEqual(entry.state, .failed)
        XCTAssertTrue(entry.error?.contains("415") ?? false)
    }

    func testRetryFailedOnlyRetriesFailedRecords() async throws {
        let library = standardLibrary()
        let uploader = FakeUploader()

        // 预置账本：PHOTO-1 失败、LIVE-1 已同步
        var seeded = SyncLedger()
        seeded.markFailed(assetID: "PHOTO-1", name: "iostest-a.jpg", modificationDate: nil, error: "旧的失败", library: "gallery")
        seeded.markSynced(assetID: "LIVE-1", name: "iostest-livep.HEIC",
                          modificationDate: Date(timeIntervalSince1970: 2_000),
                          serverAssetID: "server-old", bytes: 64, library: "gallery")
        let ledgerFile = LedgerFile(url: directory.appendingPathComponent("SyncLedger.json"))
        try ledgerFile.save(seeded)

        let engine = makeEngine(library: library, uploader: uploader)
        await engine.bootstrap()

        await engine.retryFailed()

        XCTAssertEqual(uploader.calls.map(\.name), ["iostest-a.jpg"])
        XCTAssertEqual(engine.state.phase, .finished)
        XCTAssertEqual(engine.state.succeeded, 1)
        XCTAssertEqual(loadLedger().entry(forAsset: "PHOTO-1")?.state, .done)
        // 已同步的没被碰过
        XCTAssertEqual(loadLedger().entry(forAsset: "LIVE-1")?.serverAssetID, "server-old")
    }
}
