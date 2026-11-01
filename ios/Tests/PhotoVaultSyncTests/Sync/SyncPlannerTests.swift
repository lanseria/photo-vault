//
//  SyncPlannerTests.swift
//  PhotoVaultSyncTests
//
//  规划层纯逻辑单测：过滤、资源选择、文件名 / stem、Live Photo 配对、增量判断。
//  不依赖 Photos 和网络。
//

import XCTest
@testable import PhotoVaultSync

final class SyncPlannerTests: XCTestCase {

    // MARK: - 构造工具

    private func asset(id: String = "/local/asset/1",
                       media: PhotoMediaType = .photo,
                       live: Bool = false,
                       created: Date? = nil,
                       modified: Date? = Date(timeIntervalSince1970: 1_000),
                       width: Int = 4032,
                       height: Int = 3024,
                       duration: TimeInterval = 0,
                       hidden: Bool = false) -> PhotoAssetInfo {
        PhotoAssetInfo(id: id,
                       mediaType: media,
                       creationDate: created,
                       modificationDate: modified,
                       isHidden: hidden,
                       isLivePhoto: live,
                       pixelWidth: width,
                       pixelHeight: height,
                       duration: duration)
    }

    private func resource(_ kind: PhotoResourceKind, _ name: String,
                          uti: String = "") -> PhotoResourceInfo {
        PhotoResourceInfo(kind: kind, originalFilename: name, uniformTypeIdentifier: uti)
    }

    // MARK: - 普通照片 / 视频

    func testPlainPhotoUsesOriginalFilename() throws {
        let item = asset()
        let group = try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [resource(.photo, "iostest-a.jpg", uti: "public.jpeg")],
            options: SyncOptions(), batchID: "BATCH-1"))
        XCTAssertEqual(group.kind, .photo)
        XCTAssertEqual(group.files.count, 1)
        XCTAssertEqual(group.displayName, "iostest-a.jpg")
        XCTAssertEqual(group.files[0].name, "iostest-a.jpg")
        XCTAssertEqual(group.files[0].batchID, "BATCH-1")
        XCTAssertEqual(group.matchNameKey, "iostest-a.jpg")
        XCTAssertTrue(group.allowedServerKinds.contains("photo"))
    }

    func testUneditedPhotoUsesPhotoResource() throws {
        // 未编辑照片只有 .photo，它就是原始文件
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(),
            resources: [
                resource(.photo, "IMG_0009.HEIC", uti: "public.heic"),
                resource(.fullSizePhoto, "IMG_0009.HEIC", uti: "public.heic"),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].resource.kind, .photo)
        XCTAssertEqual(group.files[0].name, "IMG_0009.HEIC")
    }

    func testEditedPhotoPrefersAdjustmentBaseOriginal() throws {
        // 编辑过的照片：.photo / .fullSizePhoto 是渲染结果，原件在 .adjustmentBasePhoto
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(),
            resources: [
                resource(.photo, "IMG_0010.HEIC", uti: "public.heic"),
                resource(.fullSizePhoto, "IMG_0010.HEIC", uti: "public.heic"),
                resource(.adjustmentBasePhoto, "IMG_0010.HEIC", uti: "public.heic"),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].resource.kind, .adjustmentBasePhoto)
        XCTAssertEqual(group.files[0].name, "IMG_0010.HEIC")
    }

    func testEditedVideoPrefersAdjustmentBaseOriginal() throws {
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(media: .video, duration: 3),
            resources: [
                resource(.video, "IMG_0011.MOV"),
                resource(.adjustmentBaseVideo, "IMG_0011.MOV"),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].resource.kind, .adjustmentBaseVideo)
    }

    func testVideoUsesVideoResourceAndDuration() throws {
        let item = asset(media: .video, width: 320, height: 240, duration: 2.4)
        let group = try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [resource(.video, "iostest-v.mov", uti: "com.apple.quicktime-movie")],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.kind, .video)
        XCTAssertEqual(group.files[0].role, .video)
        XCTAssertEqual(group.files[0].name, "iostest-v.mov")
        XCTAssertEqual(group.durationMs, 2_400)
        XCTAssertEqual(group.allowedServerKinds, ["video"])
    }

    func testVideoSkippedWhenVideosDisabled() {
        let options = SyncOptions()
        var off = options
        off.includeVideos = false
        XCTAssertNil(SyncPlanner.group(
            for: asset(media: .video, duration: 2),
            resources: [resource(.video, "iostest-v.mov")],
            options: off))
    }

    func testPhotoSkippedWhenPhotosDisabled() {
        var options = SyncOptions()
        options.includePhotos = false
        XCTAssertNil(SyncPlanner.group(for: asset(),
                                       resources: [resource(.photo, "iostest-a.jpg")],
                                       options: options))
    }

    func testNameFallsBackWhenOriginalFilenameMissing() throws {
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(id: "ABCDEF1234/L0/001"),
            resources: [resource(.photo, "", uti: "public.heic")],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].name, "IMG_ABCDEF12.heic")
    }

    // MARK: - Live Photo

    func testLivePhotoPairsStillAndMotionWithSameBatchAndStem() throws {
        let item = asset(live: true)
        let group = try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [
                resource(.photo, "iostest-livep.HEIC", uti: "public.heic"),
                resource(.pairedVideo, "iostest-livep.MOV", uti: "com.apple.quicktime-movie"),
            ],
            options: SyncOptions(), batchID: "BATCH-LP"))
        XCTAssertEqual(group.kind, .livePhoto)
        XCTAssertEqual(group.files.count, 2)
        // 顺序硬性要求：先静帧，后动态
        XCTAssertEqual(group.files[0].role, .still)
        XCTAssertEqual(group.files[1].role, .motion)
        XCTAssertEqual(group.files[0].name, "iostest-livep.HEIC")
        XCTAssertEqual(group.files[1].name, "iostest-livep.MOV")
        // 同 batch
        XCTAssertEqual(group.files[0].batchID, "BATCH-LP")
        XCTAssertEqual(group.files[1].batchID, "BATCH-LP")
        // 只有服务器上是 live_photo 才算已存在（只有 photo 要重传补齐动态）
        XCTAssertEqual(group.allowedServerKinds, ["live_photo"])
        XCTAssertEqual(group.matchNameKey, "iostest-livep.heic")
    }

    func testEditedLivePhotoPrefersAdjustmentBasePair() throws {
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(live: true),
            resources: [
                resource(.photo, "IMG_0044.HEIC", uti: "public.heic"),
                resource(.pairedVideo, "IMG_0044.MOV"),
                resource(.adjustmentBasePhoto, "IMG_0044.HEIC", uti: "public.heic"),
                resource(.adjustmentBasePairedVideo, "IMG_0044.MOV"),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].resource.kind, .adjustmentBasePhoto)
        XCTAssertEqual(group.files[1].resource.kind, .adjustmentBasePairedVideo)
        XCTAssertEqual(group.files[1].name, "IMG_0044.MOV")
    }

    func testLivePhotoMotionNameFollowsStillStemNotPairedVideoName() throws {
        // 编辑过的 Live Photo：pairedVideo 的原始文件名可能跟静帧不同，必须用静帧 stem
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(live: true),
            resources: [
                resource(.photo, "IMG_0042.HEIC", uti: "public.heic"),
                resource(.pairedVideo, "IMG_0042_edited.MOV", uti: "com.apple.quicktime-movie"),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].name, "IMG_0042.HEIC")
        XCTAssertEqual(group.files[1].name, "IMG_0042.MOV")
    }

    func testLivePhotoWithoutPairedVideoUploadsStillOnly() throws {
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(live: true),
            resources: [resource(.photo, "IMG_0043.HEIC")],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.kind, .photo)
        XCTAssertEqual(group.files.count, 1)
        XCTAssertEqual(group.files[0].role, .still)
    }

    func testLivePhotoOffUploadsStillOnly() throws {
        var options = SyncOptions()
        options.includeLivePhotos = false
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(live: true),
            resources: [
                resource(.photo, "iostest-livep.HEIC"),
                resource(.pairedVideo, "iostest-livep.MOV"),
            ],
            options: options, batchID: "B"))
        XCTAssertEqual(group.kind, .photo)
        XCTAssertEqual(group.files.count, 1)
        XCTAssertEqual(group.files[0].role, .still)
        // 服务器上已有 photo / live_photo 都算这张已在库里
        XCTAssertEqual(Set(group.allowedServerKinds), ["photo", "live_photo", "raw"])
    }

    func testLivePhotoOnlySelectionStillUploadsStillAndMotion() throws {
        var options = SyncOptions()
        options.includePhotos = false
        options.includeLivePhotos = true
        let group = try XCTUnwrap(SyncPlanner.group(
            for: asset(live: true),
            resources: [
                resource(.photo, "iostest-livep.HEIC"),
                resource(.pairedVideo, "iostest-livep.MOV"),
            ],
            options: options, batchID: "B"))
        XCTAssertEqual(group.kind, .livePhoto)
        XCTAssertEqual(group.files.count, 2)
    }

    func testMotionNameFallsBackToAssetPrefix() throws {
        let item = asset(id: "ZZZZZZZZ99", live: true)
        let group = try XCTUnwrap(SyncPlanner.group(
            for: item,
            resources: [
                resource(.photo, ""),
                resource(.pairedVideo, ""),
            ],
            options: SyncOptions(), batchID: "B"))
        XCTAssertEqual(group.files[0].name, "IMG_ZZZZZZZZ.bin")
        XCTAssertEqual(group.files[1].name, "IMG_ZZZZZZZZ.MOV")
    }

    // MARK: - 名字 key

    func testNameKeyStripsBothSlashKindsAndLowercases() {
        XCTAssertEqual(SyncPlanner.nameKey("身材记录/IMG_4610.HEIC"), "img_4610.heic")
        XCTAssertEqual(SyncPlanner.nameKey("2026/10/17/IMG_4606.HEIC"), "img_4606.heic")
        XCTAssertEqual(SyncPlanner.nameKey("身材记录\\\\IMG_4610.HEIC"), "img_4610.heic")
        XCTAssertEqual(SyncPlanner.nameKey("  DJI_0070.MP4  "), "dji_0070.mp4")
        XCTAssertEqual(SyncPlanner.nameKey(nil), "")
        XCTAssertEqual(SyncPlanner.nameKey(""), "")
    }

    // MARK: - 选项过滤

    func testQueryFiltersHiddenAndDates() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        var query = PhotoQuery()
        XCTAssertFalse(query.accepts(asset(created: now, hidden: true)))
        query.includeHidden = true
        XCTAssertTrue(query.accepts(asset(created: now, hidden: true)))

        query.fromDate = now
        XCTAssertTrue(query.accepts(asset(created: now)))
        XCTAssertFalse(query.accepts(asset(created: now.addingTimeInterval(-60))))
        // creationDate 缺失时不拿它当过滤条件
        XCTAssertTrue(query.accepts(asset(created: nil)))
    }

    func testQueryFromSyncOptions() {
        var options = SyncOptions()
        options.includePhotos = false
        options.includeVideos = false
        options.includeLivePhotos = true
        options.includeHidden = true
        options.fromDate = Date(timeIntervalSince1970: 100)
        let query = PhotoQuery(options: options)
        XCTAssertFalse(query.includePhotos)
        XCTAssertFalse(query.includeVideos)
        XCTAssertTrue(query.includeLivePhotos)
        XCTAssertTrue(query.includeHidden)
        XCTAssertEqual(query.fromDate, options.fromDate)
        // 只勾「实况」时，Live Photo 仍要放行（否则永远传不了动态）
        XCTAssertTrue(query.accepts(asset(live: true)))
        XCTAssertFalse(query.accepts(asset(live: false)))
    }

    // MARK: - 增量

    func testLedgerIncrementalRule() {
        let modified = Date(timeIntervalSince1970: 5_000)
        let item = asset(modified: modified)
        var ledger = SyncLedger()
        XCTAssertFalse(SyncPlanner.isUpToDate(asset: item, ledger: ledger))

        ledger.markSynced(assetID: item.id, name: "IMG_0001.HEIC",
                          modificationDate: modified, serverAssetID: "S1", bytes: 100, library: "gallery")
        XCTAssertTrue(SyncPlanner.isUpToDate(asset: item, ledger: ledger))

        // 照片被编辑过 → modificationDate 变了 → 要重传
        XCTAssertFalse(SyncPlanner.isUpToDate(
            asset: asset(modified: modified.addingTimeInterval(1)), ledger: ledger))

        ledger.markFailed(assetID: item.id, name: "IMG_0001.HEIC",
                          modificationDate: modified, error: "boom", library: "gallery")
        XCTAssertFalse(SyncPlanner.isUpToDate(asset: item, ledger: ledger))
    }
}
