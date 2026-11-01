//
//  ServerIndexTests.swift
//  PhotoVaultSyncTests
//
//  服务器索引对账单测：origin 目录前缀、旋转宽高、日期 ±1 天、视频时长 ±2s、kind 白名单。
//

import XCTest
@testable import PhotoVaultSync

final class ServerIndexTests: XCTestCase {

    // MARK: - 构造工具

    /// 服务器里的 taken 是「本地墙上时间」字符串，这里统一用同一套格式化器生成，
    /// 避免测试里手写字符串和 Date 对不上
    private func remoteAsset(origin: String,
                             kind: String = "photo",
                             width: Int? = 4032,
                             height: Int? = 3024,
                             taken: Date? = nil,
                             durationMs: Int? = nil,
                             id: String = "server-1") -> RemoteAsset {
        var asset = RemoteAsset()
        asset.id = id
        asset.kind = kind
        asset.origin = origin
        asset.width = width
        asset.height = height
        asset.taken = taken.map { ServerDate.wall.string(from: $0) }
        asset.durationMs = durationMs
        return asset
    }

    private func photoGroup(name: String = "iostest-a.jpg",
                            width: Int = 4032, height: Int = 3024,
                            taken: Date? = Date(timeIntervalSince1970: 1_790_000_000),
                            batch: String = "B") throws -> UploadGroup {
        let item = PhotoAssetInfo(id: "A", mediaType: .photo,
                                  creationDate: taken, modificationDate: nil,
                                  isLivePhoto: false,
                                  pixelWidth: width, pixelHeight: height)
        return try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [PhotoResourceInfo(kind: .photo, originalFilename: name)],
            options: SyncOptions(), batchID: batch))
    }

    private func videoGroup(name: String = "iostest-v.mov",
                            durationMs: Int = 2_000,
                            taken: Date? = Date(timeIntervalSince1970: 1_790_000_000)) throws -> UploadGroup {
        let item = PhotoAssetInfo(id: "V", mediaType: .video,
                                  creationDate: taken, modificationDate: nil,
                                  isLivePhoto: false,
                                  pixelWidth: 320, pixelHeight: 240,
                                  duration: Double(durationMs) / 1000)
        return try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [PhotoResourceInfo(kind: .video, originalFilename: name)],
            options: SyncOptions(), batchID: "B"))
    }

    // MARK: - 命中

    func testMatchesByNameAndTolerantDimensions() throws {
        let group = try photoGroup(width: 640, height: 480, taken: Date(timeIntervalSince1970: 1_790_000_000))
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-a.jpg", width: 640, height: 480,
            taken: Date(timeIntervalSince1970: 1_790_000_000)))
        XCTAssertTrue(ServerIndex.matches(group: group, entry: entry))
    }

    func testRotatedDimensionsStillMatch() throws {
        // 竖拍照片服务器可能存成 4032x3024，本机是 3024x4032
        let group = try photoGroup(width: 3024, height: 4032)
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-a.jpg", width: 4032, height: 3024))
        XCTAssertTrue(ServerIndex.matches(group: group, entry: entry))
    }

    func testOriginWithDirectoryPrefixesMatches() throws {
        let group = try photoGroup(name: "IMG_4610.HEIC", width: 4032, height: 3024)
        for origin in ["身材记录/IMG_4610.HEIC", "2026/10/17/IMG_4610.HEIC", "a\\\\b\\\\IMG_4610.HEIC"] {
            let entry = ServerIndexEntry(remote: remoteAsset(
                origin: origin, width: 4032, height: 3024))
            XCTAssertTrue(ServerIndex.matches(group: group, entry: entry), "origin=\(origin)")
        }
    }

    func testTakenDateWithinOneDayMatches() throws {
        let base = Date(timeIntervalSince1970: 1_790_000_000)
        let group = try photoGroup(taken: base)
        for offset in [-86_000.0, 0, 86_000.0] {
            let entry = ServerIndexEntry(remote: remoteAsset(
                origin: "iostest-a.jpg", taken: base.addingTimeInterval(offset)))
            XCTAssertTrue(ServerIndex.matches(group: group, entry: entry), "offset=\(offset)")
        }
    }

    func testMissingServerFieldsAreNotVeto() throws {
        let group = try photoGroup()
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-a.jpg", width: nil, height: nil, taken: nil))
        XCTAssertTrue(ServerIndex.matches(group: group, entry: entry))
    }

    func testVideoDurationWithinToleranceMatches() throws {
        let group = try videoGroup(durationMs: 2_000)
        let match = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-v.mov", kind: "video", width: 320, height: 240,
            durationMs: 3_500, id: "v1"))
        XCTAssertTrue(ServerIndex.matches(group: group, entry: match))
    }

    // MARK: - 不命中

    func testWrongNameDoesNotMatch() throws {
        let group = try photoGroup(name: "iostest-a.jpg")
        let entry = ServerIndexEntry(remote: remoteAsset(origin: "other.jpg"))
        XCTAssertFalse(ServerIndex.matches(group: group, entry: entry))
    }

    func testDifferentDimensionsDoNotMatch() throws {
        let group = try photoGroup(width: 640, height: 480)
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-a.jpg", width: 4000, height: 3000))
        XCTAssertFalse(ServerIndex.matches(group: group, entry: entry))
    }

    func testDifferentDayDoesNotMatch() throws {
        let base = Date(timeIntervalSince1970: 1_790_000_000)
        let group = try photoGroup(taken: base)
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-a.jpg", taken: base.addingTimeInterval(3 * 86_400)))
        XCTAssertFalse(ServerIndex.matches(group: group, entry: entry))
    }

    func testVideoDurationOutOfToleranceDoesNotMatch() throws {
        let group = try videoGroup(durationMs: 2_000)
        let entry = ServerIndexEntry(remote: remoteAsset(
            origin: "iostest-v.mov", kind: "video", width: 320, height: 240, durationMs: 9_000))
        XCTAssertFalse(ServerIndex.matches(group: group, entry: entry))
    }

    func testKindWhitelistMatters() throws {
        // 普通照片：服务器上 photo / live_photo / raw 都算已在库里
        let photo = try photoGroup(name: "IMG_0001.HEIC")
        XCTAssertTrue(ServerIndex.matches(
            group: photo,
            entry: ServerIndexEntry(remote: remoteAsset(origin: "IMG_0001.HEIC", kind: "photo"))))
        XCTAssertTrue(ServerIndex.matches(
            group: photo,
            entry: ServerIndexEntry(remote: remoteAsset(origin: "IMG_0001.HEIC", kind: "live_photo"))))
        XCTAssertFalse(ServerIndex.matches(
            group: photo,
            entry: ServerIndexEntry(remote: remoteAsset(origin: "IMG_0001.HEIC", kind: "video"))))

        // Live Photo：只有服务器上已经是 live_photo 才算同步过；
        // 只有 photo 说明上次动态没传成功，要重传补齐
        let live = try makeLiveGroup()
        XCTAssertTrue(ServerIndex.matches(
            group: live,
            entry: ServerIndexEntry(remote: remoteAsset(origin: "iostest-livep.HEIC", kind: "live_photo"))))
        XCTAssertFalse(ServerIndex.matches(
            group: live,
            entry: ServerIndexEntry(remote: remoteAsset(origin: "iostest-livep.HEIC", kind: "photo"))))
    }

    // MARK: - 索引整体查找

    func testIndexPicksCorrectCandidateAmongSameNames() throws {
        let base = Date(timeIntervalSince1970: 1_790_000_000)
        let group = try photoGroup(name: "IMG_0001.HEIC", width: 640, height: 480, taken: base)
        let entries = [
            ServerIndexEntry(remote: remoteAsset(origin: "x/IMG_0001.HEIC", width: 640, height: 480,
                                                 taken: base.addingTimeInterval(-10 * 86_400),
                                                 id: "old")),
            ServerIndexEntry(remote: remoteAsset(origin: "IMG_0001.HEIC", width: 640, height: 480,
                                                 taken: base, id: "hit")),
        ]
        let index = ServerIndex(entries: entries)
        XCTAssertEqual(index.match(group: group)?.id, "hit")
        XCTAssertEqual(index.count, 2)
    }

    func testEmptyIndexNeverMatches() throws {
        let index = ServerIndex()
        XCTAssertNil(index.match(group: try photoGroup()))
        XCTAssertTrue(index.isEmpty)
    }

    func testLivePhotoWithOnlyStillOnServerIsNotGroupMatchButMatchesStill() throws {
        // 服务器只有静帧（photo）：不能算「实况已同步」，但也绝不能只传动态
        let live = try makeLiveGroup()
        let photoEntry = ServerIndexEntry(remote: remoteAsset(origin: "iostest-livep.HEIC",
                                                              kind: "photo", id: "still-only"))
        let index = ServerIndex(entries: [photoEntry])
        XCTAssertNil(index.match(group: live), "只有静帧时整组匹配应为 nil")
        XCTAssertEqual(index.matchStill(of: live)?.id, "still-only",
                       "matchStill 要能找到那条静帧，让引擎整组跳过")
    }

    func testMatchStillFindsLivePhotoEntryUsedByGroupMatch() throws {
        let live = try makeLiveGroup()
        let liveEntry = ServerIndexEntry(remote: remoteAsset(origin: "iostest-livep.HEIC",
                                                             kind: "live_photo", id: "live"))
        let index = ServerIndex(entries: [liveEntry])
        XCTAssertEqual(index.match(group: live)?.id, "live")
        XCTAssertEqual(index.matchStill(of: live)?.id, "live")
    }

    private func makeLiveGroup() throws -> UploadGroup {
        let item = PhotoAssetInfo(id: "LP", mediaType: .photo,
                                  creationDate: Date(timeIntervalSince1970: 1_790_000_000),
                                  modificationDate: nil, isLivePhoto: true,
                                  pixelWidth: 4032, pixelHeight: 3024)
        return try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [
                PhotoResourceInfo(kind: .photo, originalFilename: "iostest-livep.HEIC"),
                PhotoResourceInfo(kind: .pairedVideo, originalFilename: "iostest-livep.MOV"),
            ],
            options: SyncOptions(), batchID: "B"))
    }
}
