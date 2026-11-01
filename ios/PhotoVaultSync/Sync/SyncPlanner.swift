//
//  SyncPlanner.swift
//  PhotoVaultSync
//
//  纯规划逻辑：把一个 PHAsset 规划成「要上传哪些文件、每个文件叫什么名字」。
//  这里不碰网络、不碰 PhotoKit 对象，全部输入输出都是值类型，单测直接构造。
//
//  三条硬规则（服务端契约，实测过）：
//    1. Live Photo = 一个 PHAsset 两个文件，必须**先静帧后动态**；
//    2. 两个文件用**同一个 batch**（每个 PHAsset 一个 UUID）和**同一个 stem**；
//    3. 动态那条的 name 用静帧 stem + ".MOV" —— 不能直接用 pairedVideo 的原始文件名，
//       相机/编辑产生的名字不一定跟静帧一致，服务端靠 (batch, stem) 配对。
//

import Foundation

// MARK: - 规划结果

enum UploadFileRole: String, Sendable, Equatable {
    case still   // 静帧（普通照片 / Live Photo 的静帧）
    case motion  // Live Photo 的动态部分
    case video   // 普通视频
}

/// 一个待上传文件
struct UploadFilePlan: Equatable, Sendable, Identifiable {
    var id: String { "\(assetIdentifier)#\(role.rawValue)" }
    var assetIdentifier: String
    var role: UploadFileRole
    /// 上传给服务端的 name（query 参数），Live Photo 两条共享 stem
    var name: String
    /// 从 PhotoKit 的哪条资源导出
    var resource: PhotoResourceInfo
    /// 同一个 PHAsset 的所有文件共用一个 batch
    var batchID: String
}

/// 一个 PHAsset 对应的上传组：普通照片/视频一条文件；Live Photo 两条且必须串行
struct UploadGroup: Equatable, Sendable, Identifiable {
    enum Kind: String, Sendable, Equatable {
        case photo
        case video
        case livePhoto
    }

    var id: String { assetIdentifier }
    var assetIdentifier: String
    var kind: Kind
    /// 上传顺序 = 数组顺序（Live Photo 静帧在前，动态在后）
    var files: [UploadFilePlan]
    var displayName: String
    var pixelWidth: Int
    var pixelHeight: Int
    var takenAt: Date?
    /// 视频时长（毫秒）；照片 / Live Photo 为 nil
    var durationMs: Int?
    var modificationDate: Date?
    /// 与服务器索引对账用的文件名 key（小写 basename）
    var matchNameKey: String
    /// 服务器上什么 kind 的条目可以认为「这张已经在库里了」
    var allowedServerKinds: [String]

    /// 服务器索引里已经存在、本轮不再上传的资产
    struct ServerSkip: Equatable, Sendable {
        var assetIdentifier: String
        var name: String
        var serverAssetID: String
        var serverKind: String
        var modificationDate: Date?
    }

    func skip(serverAssetID: String, serverKind: String) -> ServerSkip {
        ServerSkip(assetIdentifier: assetIdentifier,
                   name: displayName,
                   serverAssetID: serverAssetID,
                   serverKind: serverKind,
                   modificationDate: modificationDate)
    }
}

// MARK: - 规划器

enum SyncPlanner {

    /// 账本说「不用再传」：上次成功/跳过、之后照片没被编辑过、且目标库没换过
    static func isUpToDate(asset: PhotoAssetInfo, ledger: SyncLedger,
                           library: String = "gallery") -> Bool {
        ledger.isUpToDate(assetID: asset.id,
                          modificationDate: asset.modificationDate,
                          library: library)
    }

    /// 一个 PHAsset → 一个上传组。返回 nil 表示这轮不该传它（选项过滤掉了 / 没有可导出资源）。
    static func group(for asset: PhotoAssetInfo,
                      resources: [PhotoResourceInfo],
                      options: SyncOptions,
                      batchID: String = UUID().uuidString) -> UploadGroup? {
        switch asset.mediaType {
        case .video:
            guard options.includeVideos,
                  let video = preferredResource(for: .video, from: resources) else { return nil }
            let name = plannedName(for: .video, asset: asset, resource: video, stillStem: nil)
            return UploadGroup(assetIdentifier: asset.id,
                               kind: .video,
                               files: [UploadFilePlan(assetIdentifier: asset.id, role: .video,
                                                      name: name, resource: video, batchID: batchID)],
                               displayName: name,
                               pixelWidth: asset.pixelWidth,
                               pixelHeight: asset.pixelHeight,
                               takenAt: asset.creationDate,
                               durationMs: asset.duration > 0 ? Int((asset.duration * 1000).rounded()) : nil,
                               modificationDate: asset.modificationDate,
                               matchNameKey: nameKey(name),
                               allowedServerKinds: ["video"])

        case .photo:
            let still = preferredResource(for: .still, from: resources)
            // Live Photo：用户要实况（或至少选了照片）且动态资源也在 → 两个文件一组
            if asset.isLivePhoto, options.includeLivePhotos,
               let still, let motion = preferredResource(for: .motion, from: resources) {
                let stillName = plannedName(for: .still, asset: asset, resource: still, stillStem: nil)
                let motionName = motionName(stillName: stillName, resource: motion, asset: asset)
                let files = [
                    UploadFilePlan(assetIdentifier: asset.id, role: .still, name: stillName,
                                   resource: still, batchID: batchID),
                    UploadFilePlan(assetIdentifier: asset.id, role: .motion, name: motionName,
                                   resource: motion, batchID: batchID),
                ]
                return UploadGroup(assetIdentifier: asset.id,
                                   kind: .livePhoto,
                                   files: files,
                                   displayName: stillName,
                                   pixelWidth: asset.pixelWidth,
                                   pixelHeight: asset.pixelHeight,
                                   takenAt: asset.creationDate,
                                   durationMs: nil,
                                   modificationDate: asset.modificationDate,
                                   // 只要静帧在服务器上是 live_photo 才算「已经同步过」：
                                   // 如果服务器上只有一条 photo（上次动态没传成功），要重传补齐。
                                   matchNameKey: nameKey(stillName),
                                   allowedServerKinds: ["live_photo"])
            }
            // 普通照片，或者用户没勾「实况」的 Live Photo：只传静帧。
            // 普通照片只看 includePhotos；Live Photo 因为静帧是动态的前提，两个开关任一都放行。
            let stillAllowed = asset.isLivePhoto
                ? (options.includePhotos || options.includeLivePhotos)
                : options.includePhotos
            guard stillAllowed, let still else { return nil }
            let name = plannedName(for: .still, asset: asset, resource: still, stillStem: nil)
            return UploadGroup(assetIdentifier: asset.id,
                               kind: .photo,
                               files: [UploadFilePlan(assetIdentifier: asset.id, role: .still,
                                                      name: name, resource: still, batchID: batchID)],
                               displayName: name,
                               pixelWidth: asset.pixelWidth,
                               pixelHeight: asset.pixelHeight,
                               takenAt: asset.creationDate,
                               durationMs: nil,
                               modificationDate: asset.modificationDate,
                               matchNameKey: nameKey(name),
                               // 服务器上 photo / live_photo / raw 都算「这张已经在库里」
                               allowedServerKinds: ["photo", "live_photo", "raw"])
        }
    }

    // MARK: 资源选择

    /// 按角色挑最合适的 PhotoKit 资源，**一律优先「编辑前的原始文件」**：
    ///   * 未编辑照片只有 .photo，它本身就是原件，行为不变；
    ///   * 编辑过的照片 .photo / .fullSizePhoto 都是渲染结果，原件在 .adjustmentBasePhoto；
    ///   * 视频 / Live Photo 动态同理（adjustmentBaseVideo / adjustmentBasePairedVideo）。
    /// 这个库的核心承诺是「原件永不损坏、可迁移」，存渲染结果等于偷偷降级。
    static func preferredResource(for role: UploadFileRole,
                                  from resources: [PhotoResourceInfo]) -> PhotoResourceInfo? {
        let preference: [PhotoResourceKind]
        switch role {
        case .still:
            preference = [.adjustmentBasePhoto, .photo, .fullSizePhoto, .alternatePhoto]
        case .motion:
            preference = [.adjustmentBasePairedVideo, .pairedVideo, .fullSizePairedVideo]
        case .video:
            preference = [.adjustmentBaseVideo, .video, .fullSizeVideo]
        }
        for kind in preference {
            if let hit = resources.first(where: { $0.kind == kind }) { return hit }
        }
        return nil
    }

    // MARK: 文件名

    /// 规划上传用的文件名
    static func plannedName(for role: UploadFileRole,
                            asset: PhotoAssetInfo,
                            resource: PhotoResourceInfo,
                            stillStem: String?) -> String {
        switch role {
        case .still, .video:
            let name = baseName(resource.originalFilename)
            return name.isEmpty ? fallbackName(asset: asset, resource: resource) : name
        case .motion:
            let stem = stillStem.map { deletingExtension($0) }
                ?? deletingExtension(baseName(resource.originalFilename))
            let resolved = stem.isEmpty ? "IMG_\(String(asset.id.prefix(8)))" : stem
            return resolved + ".MOV"
        }
    }

    /// Live Photo 动态部分的文件名：静帧 stem + ".MOV"（契约要求，扩展名大写）
    static func motionName(stillName: String, resource: PhotoResourceInfo,
                           asset: PhotoAssetInfo) -> String {
        var stem = deletingExtension(stillName)
        if stem.isEmpty {
            stem = deletingExtension(baseName(resource.originalFilename))
        }
        if stem.isEmpty { stem = "IMG_\(String(asset.id.prefix(8)))" }
        return stem + ".MOV"
    }

    /// 取 basename：服务器 origin 里可能有 "/" 或 "\" 目录前缀，两种都要切
    static func baseName(_ raw: String) -> String {
        let unified = raw.replacingOccurrences(of: "\\", with: "/")
        return (unified as NSString).lastPathComponent
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 去掉扩展名（"IMG_0001.HEIC" → "IMG_0001"）
    static func deletingExtension(_ name: String) -> String {
        let stem = (name as NSString).deletingPathExtension
        return stem.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 对账 key：basename 再小写
    static func nameKey(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "" }
        return baseName(raw).lowercased()
    }

    private static func fallbackName(asset: PhotoAssetInfo,
                                     resource: PhotoResourceInfo) -> String {
        let stem = "IMG_\(String(asset.id.prefix(8)))"
        let ext = fileExtension(for: resource)
        return ext.isEmpty ? stem : "\(stem).\(ext)"
    }

    private static func fileExtension(for resource: PhotoResourceInfo) -> String {
        let ext = (resource.originalFilename as NSString).pathExtension
        if !ext.isEmpty { return ext.lowercased() }
        let uti = resource.uniformTypeIdentifier.lowercased()
        if uti.contains("heic") { return "heic" }
        if uti.contains("jpeg") { return "jpg" }
        if uti.contains("png") { return "png" }
        if uti.contains("quicktime") || uti.contains("movie") { return "mov" }
        return "bin"
    }
}
