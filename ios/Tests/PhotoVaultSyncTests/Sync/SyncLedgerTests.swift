//
//  SyncLedgerTests.swift
//  PhotoVaultSyncTests
//
//  账本单测：记录读写、原子落盘、坏文件降级、增量/失败/中断语义。
//

import XCTest
@testable import PhotoVaultSync

@MainActor
final class SyncLedgerTests: XCTestCase {

    private var directory: URL!

    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("sync-ledger-tests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: directory)
        directory = nil
        super.tearDown()
    }

    private func makeFile() -> LedgerFile {
        LedgerFile(url: directory.appendingPathComponent("SyncLedger.json"))
    }

    // MARK: - 落盘 round-trip

    func testSaveAndLoadRoundTrip() throws {
        let file = makeFile()
        var ledger = SyncLedger()
        let modified = Date(timeIntervalSince1970: 1_700_000_000)
        ledger.markSynced(assetID: "A/L0/001", name: "iostest-a.jpg",
                          modificationDate: modified, serverAssetID: "server-id-1", bytes: 1234, library: "gallery")
        ledger.markFailed(assetID: "B/L0/002", name: "iostest-b.mov",
                          modificationDate: modified, error: "boom", library: "gallery")
        try file.save(ledger)

        let loaded = file.load()
        XCTAssertEqual(loaded.records.count, 2)
        let synced = try XCTUnwrap(loaded.entry(forAsset: "A/L0/001"))
        XCTAssertEqual(synced.state, .done)
        XCTAssertEqual(synced.serverAssetID, "server-id-1")
        XCTAssertEqual(synced.name, "iostest-a.jpg")
        XCTAssertEqual(synced.bytes, 1234)
        XCTAssertEqual(synced.modificationDate, modified)
        XCTAssertEqual(synced.library, "gallery")
        XCTAssertEqual(loaded.entry(forAsset: "B/L0/002")?.state, .failed)
    }

    func testSaveOverwritesAtomicallyAndLeavesNoTempFiles() throws {
        let file = makeFile()
        var ledger = SyncLedger()
        ledger.markSkipped(assetID: "A", name: "a.jpg", modificationDate: nil, serverAssetID: "s", library: "gallery")
        try file.save(ledger)
        var second = SyncLedger()
        second.markSynced(assetID: "B", name: "b.jpg", modificationDate: nil,
                          serverAssetID: "t", bytes: 1, library: "gallery")
        try file.save(second)

        let names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        XCTAssertEqual(names, ["SyncLedger.json"])
        XCTAssertEqual(file.load().records.count, 1)
        XCTAssertNotNil(file.load().entry(forAsset: "B"))
    }

    func testLoadMissingFileReturnsEmptyLedger() {
        XCTAssertTrue(makeFile().load().records.isEmpty)
    }

    func testCorruptFileIsBackedUpAndDegradesToEmptyLedger() throws {
        let file = makeFile()
        try Data("这个不是 JSON".utf8).write(to: file.url)
        let loaded = file.load()
        XCTAssertTrue(loaded.records.isEmpty)
        let names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        XCTAssertTrue(names.contains { $0.contains("corrupt") }, "坏账本应该被备份：\(names)")
    }

    // MARK: - 状态迁移

    func testIncrementalUpToDateRule() {
        let modified = Date(timeIntervalSince1970: 2_000)
        var ledger = SyncLedger()
        ledger.markSynced(assetID: "A", name: "a.jpg", modificationDate: modified,
                          serverAssetID: "s", bytes: 10, library: "gallery")
        XCTAssertTrue(ledger.isUpToDate(assetID: "A", modificationDate: modified))
        XCTAssertFalse(ledger.isUpToDate(assetID: "A", modificationDate: Date(timeIntervalSince1970: 9_000)))
        XCTAssertFalse(ledger.isUpToDate(assetID: "missing", modificationDate: modified))

        ledger.markSkipped(assetID: "B", name: "b.jpg", modificationDate: nil, serverAssetID: "x", library: "gallery")
        XCTAssertTrue(ledger.isUpToDate(assetID: "B", modificationDate: nil, library: "gallery"))
    }

    func testSwitchingLibraryInvalidatesUpToDate() {
        let modified = Date(timeIntervalSince1970: 7_000)
        var ledger = SyncLedger()
        ledger.markSynced(assetID: "A", name: "a.jpg", modificationDate: modified,
                          serverAssetID: "s", bytes: 1, library: "gallery")
        XCTAssertTrue(ledger.isUpToDate(assetID: "A", modificationDate: modified, library: "gallery"))
        // 切到素材库：这张在素材库里还没传过，不能因为 modificationDate 没变就跳过
        XCTAssertFalse(ledger.isUpToDate(assetID: "A", modificationDate: modified, library: "material"))
        // 旧账本没有 library 字段（nil）→ 视为相册
        var legacy = SyncLedger()
        legacy.records["B"] = LedgerEntry(modificationDate: modified, state: .done,
                                          serverAssetID: "s", name: "b.jpg", bytes: 1,
                                          updatedAt: Date(), error: nil, library: nil)
        XCTAssertTrue(legacy.isUpToDate(assetID: "B", modificationDate: modified, library: "gallery"))
        XCTAssertFalse(legacy.isUpToDate(assetID: "B", modificationDate: modified, library: "material"))
    }

    func testInterruptedUploadsFallBackToPending() {
        var ledger = SyncLedger()
        ledger.markUploading(assetID: "A", name: "a.jpg", modificationDate: nil, library: "gallery")
        ledger.markSynced(assetID: "B", name: "b.jpg", modificationDate: nil,
                          serverAssetID: "s", bytes: 1, library: "gallery")
        XCTAssertTrue(ledger.markInterruptedUploadsPending())
        XCTAssertEqual(ledger.entry(forAsset: "A")?.state, .pending)
        XCTAssertEqual(ledger.entry(forAsset: "B")?.state, .done)
        // 再调一次不该有变化
        XCTAssertFalse(ledger.markInterruptedUploadsPending())
    }

    func testRemoveFinishedRecordsKeepsDone() {
        var ledger = SyncLedger()
        ledger.markSynced(assetID: "done", name: "d.jpg", modificationDate: nil,
                          serverAssetID: "s", bytes: 1, library: "gallery")
        ledger.markSkipped(assetID: "skip", name: "s.jpg", modificationDate: nil, serverAssetID: "x", library: "gallery")
        ledger.markFailed(assetID: "fail", name: "f.jpg", modificationDate: nil, error: "e", library: "gallery")
        XCTAssertEqual(ledger.removeFinishedRecords(), 2)
        XCTAssertNotNil(ledger.entry(forAsset: "done"))
        XCTAssertNil(ledger.entry(forAsset: "skip"))
        XCTAssertNil(ledger.entry(forAsset: "fail"))
    }

    func testFailedAssetIDs() {
        var ledger = SyncLedger()
        ledger.markFailed(assetID: "A", name: "a", modificationDate: nil, error: "x", library: "gallery")
        ledger.markSynced(assetID: "B", name: "b", modificationDate: nil,
                          serverAssetID: "s", bytes: 0, library: "gallery")
        XCTAssertEqual(ledger.failedAssetIDs, ["A"])
    }

    func testStatisticsOnlyCountsKnownAssets() {
        var ledger = SyncLedger()
        ledger.markSynced(assetID: "A", name: "a", modificationDate: nil,
                          serverAssetID: "s", bytes: 100, library: "gallery")
        ledger.markSkipped(assetID: "B", name: "b", modificationDate: nil, serverAssetID: "s", library: "gallery")
        ledger.markFailed(assetID: "C", name: "c", modificationDate: nil, error: "e", library: "gallery")
        ledger.markSynced(assetID: "Z", name: "z", modificationDate: nil,
                          serverAssetID: "s", bytes: 999, library: "gallery")   // 相册里已经没有了

        let counts = ledger.statistics(for: ["A", "B", "C"], library: "gallery")
        XCTAssertEqual(counts.synced, 2)
        XCTAssertEqual(counts.failed, 1)
        XCTAssertEqual(counts.bytes, 100)
    }

    func testMarkPendingClearsErrorButKeepsServerID() {
        var ledger = SyncLedger()
        ledger.markSynced(assetID: "A", name: "a", modificationDate: nil,
                          serverAssetID: "server-1", bytes: 5, library: "gallery")
        ledger.markFailed(assetID: "A", name: "a", modificationDate: nil, error: "boom", library: "gallery")
        ledger.markPending(assetID: "A")
        let entry = ledger.entry(forAsset: "A")
        XCTAssertEqual(entry?.state, .pending)
        XCTAssertNil(entry?.error)
        XCTAssertEqual(entry?.serverAssetID, "server-1")
    }
}
