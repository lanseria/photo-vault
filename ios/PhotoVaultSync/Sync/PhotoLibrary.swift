//
//  PhotoLibrary.swift
//  PhotoVaultSync
//
//  PhotoKit 的唯一出口。Sync 引擎只依赖 PhotoLibraryProviding 协议：
//    1. 规划/账本/匹配是纯逻辑，单测塞 FakePhotoLibrary 就能跑，不需要真的授权和相册；
//    2. 授权、枚举、资源描述、导出原件这些「脏活」只在 SystemPhotoLibrary 里做，
//       出问题时只需要看这一个文件。
//
//  文件名一律取 PHAssetResource.originalFilename（不是 PHAsset 的展示名）：
//  模拟器 / 相机导入后 PHAsset 的展示名可能被改成 IMG_00xx，而 originalFilename
//  才是真正的原始文件名 —— Live Photo 的同 stem 配对、服务器 origin 对账都依赖它。
//

import Foundation
import Photos
import PhotosUI
import UIKit

// MARK: - 纯数据（可跨线程传递，不持有 PhotoKit 对象）

enum PhotoMediaType: String, Sendable, Equatable {
    case photo
    case video
}

/// 我们关心的 PHAssetResource 类型。照片可能有「当前版本 / 原始文件」多条，
/// Live Photo 还多一条 pairedVideo；其它（adjustmentData、audio…）一律归 .other。
///
/// 原始文件语义（重要，别按名字猜）：
///   * 未编辑的照片：只有 .photo，它就是原始文件；
///   * 编辑过的照片：.photo 是当前渲染版本，.fullSizePhoto 是当前版本的「全尺寸渲染」，
///     **编辑前的原始文件是 .adjustmentBasePhoto**（iOS 10+）；
///   * 视频 / Live Photo 动态同理：.adjustmentBaseVideo / .adjustmentBasePairedVideo 才是原件。
/// 这个库要存的是「原件永不损坏」的那个原件，所以偏好顺序里 adjustmentBase* 排第一。
enum PhotoResourceKind: String, Sendable, Equatable {
    case adjustmentBasePhoto
    case photo
    case fullSizePhoto
    case alternatePhoto
    case adjustmentBasePairedVideo
    case pairedVideo
    case fullSizePairedVideo
    case adjustmentBaseVideo
    case video
    case fullSizeVideo
    case other
}

struct PhotoResourceInfo: Equatable, Sendable {
    var kind: PhotoResourceKind
    /// PHAssetResource.originalFilename（真正的原始文件名）
    var originalFilename: String
    var uniformTypeIdentifier: String = ""
}

/// 一「条」相册资产（一个 PHAsset）。不含资源列表，枚举 2 万张时保持轻量；
/// 需要文件名时才单独拉 resources(for:)。
struct PhotoAssetInfo: Equatable, Sendable, Identifiable {
    /// PHAsset.localIdentifier：账本主键
    var id: String
    var mediaType: PhotoMediaType
    var creationDate: Date?
    var modificationDate: Date?
    var isHidden: Bool = false
    /// Live Photo（静帧 + 动态配对）。照片的 mediaType 仍是 .photo
    var isLivePhoto: Bool = false
    var pixelWidth: Int = 0
    var pixelHeight: Int = 0
    /// 视频时长（秒）；照片为 0
    var duration: TimeInterval = 0
}

/// 扫描条件。只放「相册侧」的过滤条件，Live Photo 要不要带动态部分由规划器决定。
struct PhotoQuery: Equatable, Sendable {
    var includePhotos = true
    var includeVideos = true
    var includeLivePhotos = true
    var includeHidden = false
    var fromDate: Date?

    init(includePhotos: Bool = true,
         includeVideos: Bool = true,
         includeLivePhotos: Bool = true,
         includeHidden: Bool = false,
         fromDate: Date? = nil) {
        self.includePhotos = includePhotos
        self.includeVideos = includeVideos
        self.includeLivePhotos = includeLivePhotos
        self.includeHidden = includeHidden
        self.fromDate = fromDate
    }

    init(options: SyncOptions) {
        self.includePhotos = options.includePhotos
        self.includeVideos = options.includeVideos
        self.includeLivePhotos = options.includeLivePhotos
        self.includeHidden = options.includeHidden
        self.fromDate = options.fromDate
    }

    /// 这条资产是否值得进规划（纯函数，单测直接调）
    func accepts(_ asset: PhotoAssetInfo) -> Bool {
        if asset.isHidden && !includeHidden { return false }
        switch asset.mediaType {
        case .photo:
            // Live Photo 用 includeLivePhotos / includePhotos 任一放行：
            // 只勾「实况」时也要同步静帧（服务端必须收到静帧才能合成 live_photo）
            let allowed = asset.isLivePhoto ? (includeLivePhotos || includePhotos) : includePhotos
            guard allowed else { return false }
        case .video:
            guard includeVideos else { return false }
        }
        if let fromDate, let created = asset.creationDate, created < fromDate { return false }
        return true
    }
}

enum PhotoLibraryError: LocalizedError, Equatable {
    case assetNotFound
    case resourceNotFound(String)
    case exportFailed(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .assetNotFound:
            return "相册里找不到这条资产（可能已被删除）"
        case .resourceNotFound(let name):
            return "找不到可导出的原始资源：\(name)"
        case .exportFailed(let message):
            return "导出原件失败：\(message)"
        case .cancelled:
            return "已取消"
        }
    }

    /// iCloud 下载失败、磁盘临时错误都值得再试一次
    var isRetryable: Bool {
        if case .exportFailed = self { return true }
        return false
    }
}

// MARK: - 协议

/// 引擎对相册的全部依赖。@MainActor：引擎本身在主线程，实现内部自己把重活丢到后台线程。
@MainActor
protocol PhotoLibraryProviding: AnyObject {
    func authorizationStatus() -> PhotoAuthorizationStatus
    func requestAuthorization() async -> PhotoAuthorizationStatus
    /// 部分授权时让用户重选可见照片（没有 UI 的上下文时静默返回）
    func presentLimitedLibraryPicker() async
    func fetchAssets(matching query: PhotoQuery) async -> [PhotoAssetInfo]
    /// 某个 PHAsset 的全部资源（含 originalFilename）
    func resources(for assetID: String) async -> [PhotoResourceInfo]
    /// 把指定资源导出到 Caches/Uploads/ 下的临时文件，返回本地 URL
    func exportOriginal(of assetID: String, resource: PhotoResourceInfo, allowNetwork: Bool) async throws -> URL
}

// MARK: - 真实实现

@MainActor
final class SystemPhotoLibrary: PhotoLibraryProviding {

    /// 导出文件的临时目录；init 时清空，避免上次进程留下的半截文件占满缓存
    private let uploadDirectory: URL

    init() {
        let fm = FileManager.default
        let caches = fm.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? fm.temporaryDirectory
        uploadDirectory = caches.appendingPathComponent("Uploads", isDirectory: true)
        try? fm.createDirectory(at: uploadDirectory, withIntermediateDirectories: true)
        Self.removeStaleExports(in: uploadDirectory)
    }

    // MARK: 授权

    func authorizationStatus() -> PhotoAuthorizationStatus {
        Self.map(PHPhotoLibrary.authorizationStatus(for: .readWrite))
    }

    func requestAuthorization() async -> PhotoAuthorizationStatus {
        let status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        return Self.map(status)
    }

    func presentLimitedLibraryPicker() async {
        guard let controller = Self.topViewController() else {
            AppLog.shared.add(.warn, "没有可用的顶层控制器，跳过相册范围选择器")
            return
        }
        // 带 completion 的版本被 Swift 导入成 async：等用户选完再返回，然后由引擎刷新统计
        _ = await PHPhotoLibrary.shared().presentLimitedLibraryPicker(from: controller)
    }

    // MARK: 枚举

    func fetchAssets(matching query: PhotoQuery) async -> [PhotoAssetInfo] {
        // 枚举 2 万条 + 读元数据在主线程会明显卡 UI，丢到后台
        await Task.detached(priority: .utility) { [query] in
            let options = PHFetchOptions()
            options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: true)]
            options.includeHiddenAssets = query.includeHidden
            if let from = query.fromDate {
                options.predicate = NSPredicate(format: "creationDate >= %@", from as NSDate)
            }
            let result = PHAsset.fetchAssets(with: options)
            var assets: [PhotoAssetInfo] = []
            assets.reserveCapacity(result.count)
            result.enumerateObjects { asset, _, _ in
                let info = Self.info(from: asset)
                if query.accepts(info) { assets.append(info) }
            }
            return assets
        }.value
    }

    func resources(for assetID: String) async -> [PhotoResourceInfo] {
        await Task.detached(priority: .utility) {
            guard let asset = Self.fetchAsset(assetID) else { return [] }
            return PHAssetResource.assetResources(for: asset).map { resource in
                PhotoResourceInfo(kind: Self.kind(of: resource.type),
                                  originalFilename: resource.originalFilename,
                                  uniformTypeIdentifier: resource.uniformTypeIdentifier)
            }
        }.value
    }

    // MARK: 导出

    func exportOriginal(of assetID: String, resource: PhotoResourceInfo,
                        allowNetwork: Bool) async throws -> URL {
        guard let asset = Self.fetchAsset(assetID) else { throw PhotoLibraryError.assetNotFound }
        let candidates = PHAssetResource.assetResources(for: asset)
        // 先按 (类型 + 原始文件名) 精确找；找不到就按类型找第一条，
        // 用户在两次扫描之间编辑了照片时类型会变，精确匹配不能是唯一退路。
        let exact = candidates.first {
            Self.kind(of: $0.type) == resource.kind
                && $0.originalFilename == resource.originalFilename
        }
        let fallback = candidates.first { Self.kind(of: $0.type) == resource.kind }
            ?? Self.preferredResource(for: resource.kind, in: candidates)
        guard let chosen = exact ?? fallback else {
            throw PhotoLibraryError.resourceNotFound(resource.originalFilename)
        }

        let fileURL = uploadDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension(Self.fileExtension(for: chosen, fallback: resource))

        let options = PHAssetResourceRequestOptions()
        // iCloud 原件还没下载时，允许 PhotoKit 现拉；用户关了「允许下载」就快速失败
        options.isNetworkAccessAllowed = allowNetwork

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            PHAssetResourceManager.default().writeData(for: chosen, toFile: fileURL,
                                                       options: options) { error in
                if let error {
                    continuation.resume(throwing: PhotoLibraryError.exportFailed(error.localizedDescription))
                } else {
                    continuation.resume()
                }
            }
        }

        let size = (try? FileManager.default.attributesOfItem(atPath: fileURL.path)[.size] as? Int64) ?? 0
        guard size > 0 else {
            try? FileManager.default.removeItem(at: fileURL)
            throw PhotoLibraryError.exportFailed("导出的文件是空的")
        }
        return fileURL
    }

    // MARK: 内部工具

    private nonisolated static func fetchAsset(_ localIdentifier: String) -> PHAsset? {
        guard !localIdentifier.isEmpty else { return nil }
        return PHAsset.fetchAssets(withLocalIdentifiers: [localIdentifier], options: nil).firstObject
    }

    private nonisolated static func info(from asset: PHAsset) -> PhotoAssetInfo {
        PhotoAssetInfo(
            id: asset.localIdentifier,
            mediaType: asset.mediaType == .video ? .video : .photo,
            creationDate: asset.creationDate,
            modificationDate: asset.modificationDate,
            isHidden: asset.isHidden,
            isLivePhoto: asset.mediaSubtypes.contains(.photoLive),
            pixelWidth: asset.pixelWidth,
            pixelHeight: asset.pixelHeight,
            duration: asset.mediaType == .video ? asset.duration : 0
        )
    }

    private static func map(_ status: PHAuthorizationStatus) -> PhotoAuthorizationStatus {
        switch status {
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        case .denied: return .denied
        case .authorized: return .authorized
        case .limited: return .limited
        @unknown default: return .unknown
        }
    }

    private nonisolated static func kind(of type: PHAssetResourceType) -> PhotoResourceKind {
        switch type {
        case .adjustmentBasePhoto: return .adjustmentBasePhoto
        case .photo: return .photo
        case .fullSizePhoto: return .fullSizePhoto
        case .alternatePhoto: return .alternatePhoto
        case .adjustmentBasePairedVideo: return .adjustmentBasePairedVideo
        case .pairedVideo: return .pairedVideo
        case .fullSizePairedVideo: return .fullSizePairedVideo
        case .adjustmentBaseVideo: return .adjustmentBaseVideo
        case .video: return .video
        case .fullSizeVideo: return .fullSizeVideo
        default: return .other
        }
    }

    /// 导出时精确匹配失败后的兜底：按「原件优先」的顺序挑一条。
    /// 顺序必须跟 SyncPlanner.preferredResource 保持一致（那里是语义的唯一来源）。
    private nonisolated static func preferredResource(for kind: PhotoResourceKind,
                                                      in candidates: [PHAssetResource]) -> PHAssetResource? {
        let preference: [PhotoResourceKind]
        switch kind {
        case .adjustmentBasePhoto, .photo, .fullSizePhoto, .alternatePhoto:
            preference = [.adjustmentBasePhoto, .photo, .fullSizePhoto, .alternatePhoto]
        case .adjustmentBasePairedVideo, .pairedVideo, .fullSizePairedVideo:
            preference = [.adjustmentBasePairedVideo, .pairedVideo, .fullSizePairedVideo]
        case .adjustmentBaseVideo, .video, .fullSizeVideo:
            preference = [.adjustmentBaseVideo, .video, .fullSizeVideo]
        case .other:
            return nil
        }
        for preferred in preference {
            if let hit = candidates.first(where: { Self.kind(of: $0.type) == preferred }) {
                return hit
            }
        }
        return nil
    }

    /// 临时文件扩展名：优先用 originalFilename 的扩展名，其次按 UTI 猜，最后 .bin
    private nonisolated static func fileExtension(for resource: PHAssetResource, fallback: PhotoResourceInfo) -> String {
        let name = resource.originalFilename
        let ext = (name as NSString).pathExtension
        if !ext.isEmpty { return ext.lowercased() }
        let uti = fallback.uniformTypeIdentifier.lowercased()
        if uti.contains("heic") { return "heic" }
        if uti.contains("jpeg") { return "jpg" }
        if uti.contains("png") { return "png" }
        if uti.contains("quicktime") || uti.contains("movie") { return "mov" }
        return "bin"
    }

    private nonisolated static func removeStaleExports(in directory: URL) {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: directory.path) else { return }
        for name in names {
            try? fm.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    private static func topViewController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let window = scenes.flatMap(\.windows).first { $0.isKeyWindow }
            ?? scenes.flatMap(\.windows).first
        var controller = window?.rootViewController
        while let presented = controller?.presentedViewController {
            controller = presented
        }
        return controller
    }
}
