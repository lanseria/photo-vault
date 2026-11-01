//
//  RemoteImageStore.swift
//  PhotoVaultSync
//
//  服务器缩略图 / 详情大图的内存缓存与加载器（NSCache + 同一 URL 合并并发请求）。
//
//  ── WebP 结论（实测）──────────────────────────────────────────────
//  服务端 /thumbs/{sha}.webp 是 VP8 编码的 WebP（512×384，约 12KB）。
//  实测：模拟器 iPhone 17 Pro / iOS 26.2 上 UIImage(data:) 解码成功，图库网格能出图；
//  WebP 解码器是 iOS 14+ 内置的（ImageIO），所以这条主路径是安全的。
//  列表缩略图优先用 WebP，比 /preview/{id}（JPEG，≤2048px、约 600KB）省一个数量级流量。
//  为了不在更旧的系统上白屏，这里保留运行时兜底：某次 thumb 解码返回 nil 就把
//  webpSupported 置 false，本机后续全部改用 /preview/{id}，并写一条诊断日志（设置页可见）。
//  大图（详情页）仍然优先 /preview/{id}；视频的 preview 服务端生成不了（500），
//  会自动回退到缩略图并显示占位，不阻塞界面。
//
//  ── 降采样 ───────────────────────────────────────────────────────
//  18k+ 张的服务器库，网格里 512px 的缩略图直接解码成 UIImage 会白占 2~3 倍内存
//  （512×384 ≈ 0.75MB/张）。这里统一走 ImageIO 的 CGImageSourceCreateThumbnailAtIndex：
//  maxPixelSize = 400（≈屏宽/3×scale，见 RemoteImageMetrics），只解码网格实际需要的像素，
//  解码尺寸在 os.Logger 里有记录（category RemoteImage）。详情大图仍按 2048px 上限解码。
//

import Foundation
import OSLog
import SwiftUI
import UIKit

/// 图片解码尺寸的约定
enum RemoteImageMetrics {
    /// 网格缩略图解码上限：≈屏宽/3×scale（iPhone 402pt@3x → 402px，取 400），封顶 480。
    /// 服务端缩略图本身是 512px，降采样后可省 ~40% 解码内存，18k 库滚动时尤其明显。
    static let thumbnailMaxPixelSize = 400
    /// 详情大图上限（服务端 /preview 本身就是 ≤2048px，这里只做兜底）
    static let previewMaxPixelSize = 2048
}

@MainActor
final class RemoteImageStore {
    static let shared = RemoteImageStore()

    private let cache = NSCache<NSString, UIImage>()
    private var inflight: [String: Task<UIImage?, Never>] = [:]
    private var failedKeys: Set<String> = []

    /// 系统日志里留一份实测证据（设置页诊断日志只有 AppLog，os.Logger 便于命令行验证）
    private static let logger = Logger(subsystem: "com.photovault.ios", category: "RemoteImage")

    /// WebP 解码是否可用（首次实测后确定；失败时全 App 回退 preview）
    private(set) var webpSupported = true
    private var didLogThumbResult = false

    init() {
        cache.countLimit = 600
        // 大图单张解码后约 10MB+；缩略图降采样后约 0.4MB。上限 100MB，超出自动淘汰
        cache.totalCostLimit = 100 * 1024 * 1024
    }

    // MARK: - 对外

    /// 网格缩略图：优先 WebP thumb（降采样），失败回退 preview
    func thumbnail(thumb: URL?, preview: URL?,
                   maxPixelSize: Int = RemoteImageMetrics.thumbnailMaxPixelSize) async -> UIImage? {
        if let thumb, webpSupported {
            if let image = await load(thumb, maxPixelSize: maxPixelSize) { return image }
            webpSupported = false
            AppLog.shared.add(.warn, "缩略图 WebP 解码失败，本机回退 /preview（大图流量）")
        }
        if let preview {
            return await load(preview, maxPixelSize: RemoteImageMetrics.previewMaxPixelSize)
        }
        return nil
    }

    /// 详情大图：优先 preview，失败回退 thumb
    func preview(primary: URL?, fallback: URL?) async -> UIImage? {
        if let primary,
           let image = await load(primary, maxPixelSize: RemoteImageMetrics.previewMaxPixelSize) {
            return image
        }
        if let fallback, webpSupported,
           let image = await load(fallback, maxPixelSize: RemoteImageMetrics.thumbnailMaxPixelSize) {
            return image
        }
        return nil
    }

    /// 失败重试（网格里点按失败占位时调用）：清掉失败标记，下次请求会重新拉
    func retry(thumb: URL?, preview: URL?) {
        for url in [thumb, preview].compactMap({ $0 }) {
            failedKeys.remove(url.absoluteString)
        }
    }

    /// 下拉刷新 / 切换服务器后清掉全部失败记录
    func resetFailures() {
        failedKeys.removeAll()
    }

    // MARK: - 内部

    private func load(_ url: URL, maxPixelSize: Int) async -> UIImage? {
        let keyString = cacheKey(url, maxPixelSize: maxPixelSize)
        let key = keyString as NSString
        if let cached = cache.object(forKey: key) { return cached }
        if let task = inflight[keyString] { return await task.value }
        if failedKeys.contains(keyString) { return nil }

        let isThumb = url.path.contains("/thumbs/")
        let task = Task<UIImage?, Never> { await Self.fetch(url, maxPixelSize: maxPixelSize) }
        inflight[keyString] = task
        let image = await task.value
        inflight[keyString] = nil
        // 切 Tab / 滚动导致视图销毁时任务会被取消：不要把图片记成「失败」，
        // 否则占位图会一直留着，直到下拉刷新才重试。
        if Task.isCancelled { return image }

        if let image {
            cache.setObject(image, forKey: key, cost: Self.cost(of: image))
            if isThumb {
                let outW = image.cgImage?.width ?? Int(image.size.width)
                let outH = image.cgImage?.height ?? Int(image.size.height)
                Self.logger.debug("thumb decode maxPixelSize=\(maxPixelSize, privacy: .public) out=\(outW, privacy: .public)x\(outH, privacy: .public)")
                if !didLogThumbResult {
                    didLogThumbResult = true
                    // 缩略图能解 = WebP 主路径可用（同时留下降采样尺寸证据）
                    AppLog.shared.add(.success, "缩略图解码成功（maxPixelSize=\(maxPixelSize) → \(outW)×\(outH)）")
                    Self.logger.info("thumb webp decode ok maxPixelSize=\(maxPixelSize, privacy: .public) out=\(outW, privacy: .public)x\(outH, privacy: .public)")
                }
            }
            return image
        }

        if failedKeys.count > 500 { failedKeys.removeAll() }
        failedKeys.insert(keyString)
        if isThumb && !didLogThumbResult {
            didLogThumbResult = true
            Self.logger.error("thumb decode failed url=\(url.lastPathComponent, privacy: .public)")
            AppLog.shared.add(.warn, "缩略图解码失败（\(url.lastPathComponent)）")
        }
        return nil
    }

    /// 真正下载 + 解码。nonisolated：下载和解码都发生在主线程之外。
    /// maxPixelSize 走 ImageIO 降采样（kCGImageSourceCreateThumbnailFromImageAlways），
    /// 保证深列表里只保留网格需要的像素。
    private nonisolated static func fetch(_ url: URL, maxPixelSize: Int) async -> UIImage? {
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        request.cachePolicy = .returnCacheDataElseLoad
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode) else { return nil }

            guard let source = CGImageSourceCreateWithData(
                data as CFData,
                [kCGImageSourceShouldCache: false] as CFDictionary
            ) else {
                // 数据不是可识别的图片（例如服务端 500 的文本体）
                return nil
            }

            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
            ]
            if let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) {
                return UIImage(cgImage: cgImage)
            }
            // 极少数格式不支持缩略图接口时退回完整解码
            if let cgImage = CGImageSourceCreateImageAtIndex(
                source, 0,
                [kCGImageSourceShouldCacheImmediately: true] as CFDictionary
            ) {
                return UIImage(cgImage: cgImage)
            }
            return UIImage(data: data)
        } catch {
            return nil
        }
    }

    private func cacheKey(_ url: URL, maxPixelSize: Int) -> String {
        "\(url.absoluteString)#\(maxPixelSize)"
    }

    private static func cost(of image: UIImage) -> Int {
        guard let cg = image.cgImage else { return 1 }
        return max(1, cg.bytesPerRow * cg.height)
    }
}

// MARK: - 详情页大图视图

/// 从服务器加载一张大图：网格里用 thumb，详情用 preview（由 prefersPreview 控制）
struct RemoteImage: View {
    let thumbURL: URL?
    let previewURL: URL?
    var prefersPreview = false
    var contentMode: ContentMode = .fill
    var showsProgress = true

    @State private var image: UIImage?
    @State private var isLoading = true

    private var taskKey: String {
        let t = thumbURL?.absoluteString ?? "-"
        let p = previewURL?.absoluteString ?? "-"
        return "\(t)|\(p)|\(prefersPreview)"
    }

    var body: some View {
        ZStack {
            Rectangle().fill(Color.white.opacity(0.045))
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: contentMode)
            } else if isLoading && showsProgress {
                ProgressView()
                    .controlSize(.small)
                    .tint(Color.white.opacity(0.45))
            } else {
                Image(systemName: "photo")
                    .font(.system(size: 17, weight: .light))
                    .foregroundStyle(Color.white.opacity(0.22))
            }
        }
        .clipped()
        .task(id: taskKey) {
            isLoading = true
            let loaded: UIImage?
            if prefersPreview {
                loaded = await RemoteImageStore.shared.preview(primary: previewURL, fallback: thumbURL)
            } else {
                loaded = await RemoteImageStore.shared.thumbnail(thumb: thumbURL, preview: previewURL)
            }
            image = loaded
            isLoading = false
        }
        .accessibilityHidden(true)
    }
}
